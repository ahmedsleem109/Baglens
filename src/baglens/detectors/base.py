"""Detector protocol and the shared online primitives.

**The constraint, restated because it is the whole design:** every detector is a
single-pass online algorithm with bounded state. No detector may buffer the
recording, require the end time, or make a second pass. State is a fixed-size
struct so it can be checkpointed and so the same code runs on a live subscription.

If you find yourself wanting the full array, you are writing the offline version.
"""

from __future__ import annotations

import math
from collections import deque
from typing import Protocol

from ..models import Finding


class Welford:
    """Online mean/variance. Fixed state: 3 floats."""

    __slots__ = ("n", "mean", "m2")

    def __init__(self) -> None:
        self.n = 0
        self.mean = 0.0
        self.m2 = 0.0

    def push(self, x: float) -> None:
        self.n += 1
        d = x - self.mean
        self.mean += d / self.n
        self.m2 += d * (x - self.mean)

    @property
    def variance(self) -> float:
        return self.m2 / (self.n - 1) if self.n > 1 else 0.0

    @property
    def std(self) -> float:
        return math.sqrt(self.variance)

    @property
    def cv(self) -> float:
        return self.std / self.mean if self.mean > 0 else 0.0


class RollingWelford:
    """Welford over a fixed window. State: window floats + 3 scalars.

    Recomputes from the ring on eviction rather than using the numerically
    unstable subtract-the-old-sample trick — the window is small (200) and
    correctness beats cleverness in a detector nobody can eyeball.
    """

    __slots__ = ("window", "buf", "_sum", "_sumsq")

    def __init__(self, window: int) -> None:
        self.window = window
        self.buf: deque[float] = deque(maxlen=window)
        self._sum = 0.0
        self._sumsq = 0.0

    def push(self, x: float) -> None:
        if len(self.buf) == self.window:
            old = self.buf[0]
            self._sum -= old
            self._sumsq -= old * old
        self.buf.append(x)
        self._sum += x
        self._sumsq += x * x

    @property
    def n(self) -> int:
        return len(self.buf)

    @property
    def mean(self) -> float:
        return self._sum / self.n if self.n else 0.0

    @property
    def variance(self) -> float:
        if self.n < 2:
            return 0.0
        v = (self._sumsq - self._sum * self._sum / self.n) / (self.n - 1)
        return max(v, 0.0)

    @property
    def std(self) -> float:
        return math.sqrt(self.variance)

    @property
    def cv(self) -> float:
        m = self.mean
        return self.std / m if m > 0 else 0.0


class Ewma:
    """Exponentially weighted mean. State: 2 floats."""

    __slots__ = ("alpha", "value", "n")

    def __init__(self, alpha: float) -> None:
        self.alpha = alpha
        self.value = 0.0
        self.n = 0

    def push(self, x: float) -> float:
        self.n += 1
        if self.n == 1:
            self.value = x
        else:
            self.value += self.alpha * (x - self.value)
        return self.value


class LogHistogram:
    """Log-spaced histogram of inter-arrival times. State: ``bins`` ints.

    The mean inter-arrival is destroyed by exactly the gaps we are looking for,
    so the cadence baseline uses the modal bin instead.
    """

    __slots__ = ("bins", "lo", "hi", "_log_lo", "_scale", "counts", "total")

    def __init__(self, bins: int, lo: float, hi: float) -> None:
        self.bins = bins
        self.lo = lo
        self.hi = hi
        self._log_lo = math.log(lo)
        self._scale = bins / (math.log(hi) - self._log_lo)
        self.counts = [0] * bins
        self.total = 0

    def push(self, dt: float) -> None:
        if dt <= 0:
            return
        idx = int((math.log(min(max(dt, self.lo), self.hi)) - self._log_lo) * self._scale)
        idx = min(max(idx, 0), self.bins - 1)
        self.counts[idx] += 1
        self.total += 1

    def mode(self) -> float:
        """Geometric centre of the fullest bin."""
        if not self.total:
            return 0.0
        i = max(range(self.bins), key=self.counts.__getitem__)
        step = (math.log(self.hi) - self._log_lo) / self.bins
        return math.exp(self._log_lo + step * (i + 0.5))


def theil_sen(xs: list[float], ys: list[float]) -> float:
    """Median of pairwise slopes. Robust to the single huge gap that wrecks OLS.

    O(n^2) in the number of buckets, which is 30. That is the point of bucketing.
    """
    n = len(xs)
    if n < 2:
        return 0.0
    slopes = []
    for i in range(n):
        for j in range(i + 1, n):
            dx = xs[j] - xs[i]
            if dx != 0:
                slopes.append((ys[j] - ys[i]) / dx)
    if not slopes:
        return 0.0
    slopes.sort()
    m = len(slopes)
    return slopes[m // 2] if m % 2 else 0.5 * (slopes[m // 2 - 1] + slopes[m // 2])


def kendall_tau_p(xs: list[float], ys: list[float]) -> tuple[float, float]:
    """Kendall's tau and a normal-approximation two-sided p-value.

    Hand-rolled so the detector has no SciPy dependency on the hot path and can be
    lifted onto a device unchanged.
    """
    n = len(xs)
    if n < 4:
        return 0.0, 1.0
    concordant = discordant = 0
    for i in range(n):
        for j in range(i + 1, n):
            dx = xs[j] - xs[i]
            dy = ys[j] - ys[i]
            s = dx * dy
            if s > 0:
                concordant += 1
            elif s < 0:
                discordant += 1
    total = concordant + discordant
    if total == 0:
        return 0.0, 1.0
    tau = (concordant - discordant) / total
    var = (2 * (2 * n + 5)) / (9 * n * (n - 1))
    z = tau / math.sqrt(var) if var > 0 else 0.0
    p = math.erfc(abs(z) / math.sqrt(2.0))
    return tau, p


class Detector(Protocol):
    """Single-pass, bounded-state. Fed one arrival at a time, in log-time order."""

    name: str

    def on_arrival(self, topic: str, t: float, pub_t: float, size: int) -> None:
        """``t`` and ``pub_t`` are seconds from the first arrival in the stream."""
        ...

    def finalize(self, t_end: float) -> list[Finding]:
        """Called once when the stream ends. Must not need anything it did not keep."""
        ...

    def state_bytes(self) -> int:
        """Approximate resident state, asserted against the 2 KB/topic edge budget."""
        ...
