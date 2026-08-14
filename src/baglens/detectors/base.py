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
from typing import Any, Protocol

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

    def to_state(self) -> dict[str, Any]:
        return {"n": self.n, "mean": self.mean, "m2": self.m2}

    @classmethod
    def from_state(cls, state: dict[str, Any]) -> Welford:
        obj = cls()
        obj.n = int(state["n"])
        obj.mean = float(state["mean"])
        obj.m2 = float(state["m2"])
        return obj


class RollingWelford:
    """Welford over a fixed window. State: window floats + 4 scalars.

    Sums are accumulated **shifted by the first sample in the window**, because the
    naive form is numerically unstable exactly where this class is used. Inter-arrival
    times cluster tightly around a large-ish value (a 200 Hz topic gives 0.005 s but a
    slow one gives 5 s), so ``sumsq`` and ``sum^2/n`` end up nearly equal and their
    difference loses most of its significant digits. On a perfectly regular topic that
    produced a variance of ~1e-12 instead of 0 — a phantom jitter floor in a detector
    whose whole job is to notice small changes in variance.

    Shifting costs one extra float and makes a constant window return exactly zero.
    """

    __slots__ = ("window", "buf", "_sum", "_sumsq", "_offset")

    def __init__(self, window: int) -> None:
        self.window = window
        self.buf: deque[float] = deque(maxlen=window)
        self._sum = 0.0
        self._sumsq = 0.0
        self._offset = 0.0

    def push(self, x: float) -> None:
        if not self.buf:
            # Re-anchor on the first sample so the shifted values stay near zero.
            self._offset = x
            self._sum = 0.0
            self._sumsq = 0.0
        elif len(self.buf) == self.window:
            old = self.buf[0] - self._offset
            self._sum -= old
            self._sumsq -= old * old
        self.buf.append(x)
        shifted = x - self._offset
        self._sum += shifted
        self._sumsq += shifted * shifted

    @property
    def n(self) -> int:
        return len(self.buf)

    @property
    def mean(self) -> float:
        return self._sum / self.n + self._offset if self.n else 0.0

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

    def to_state(self) -> dict[str, Any]:
        # The ring is the state — the running sums are derived and are restored with
        # it rather than trusted, so a checkpoint cannot carry rounding drift forward.
        # The offset travels too: re-deriving it from `buf[0]` would give a *different*
        # anchor than the live object (whose anchor may since have been evicted), and
        # the restored sums would then differ in the last bits from the uninterrupted
        # run — which the identical-findings checkpoint test would catch.
        return {"window": self.window, "buf": list(self.buf), "offset": self._offset}

    @classmethod
    def from_state(cls, state: dict[str, Any]) -> RollingWelford:
        obj = cls(int(state["window"]))
        obj._offset = float(state.get("offset", 0.0))
        for x in state["buf"]:
            value = float(x)
            obj.buf.append(value)
            shifted = value - obj._offset
            obj._sum += shifted
            obj._sumsq += shifted * shifted
        return obj


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

    def to_state(self) -> dict[str, Any]:
        return {"alpha": self.alpha, "value": self.value, "n": self.n}

    @classmethod
    def from_state(cls, state: dict[str, Any]) -> Ewma:
        obj = cls(float(state["alpha"]))
        obj.value = float(state["value"])
        obj.n = int(state["n"])
        return obj


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

    def to_state(self) -> dict[str, Any]:
        return {"bins": self.bins, "lo": self.lo, "hi": self.hi,
                "counts": list(self.counts), "total": self.total}

    @classmethod
    def from_state(cls, state: dict[str, Any]) -> LogHistogram:
        obj = cls(int(state["bins"]), float(state["lo"]), float(state["hi"]))
        obj.counts = [int(c) for c in state["counts"]]
        obj.total = int(state["total"])
        return obj


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

    def to_state(self) -> dict[str, Any]:
        """The whole detector as JSON-safe data.

        "Fixed-size struct, serialisable, so it can be checkpointed" is a claim this
        library makes about itself; this is where it is paid for. A restored detector
        must produce byte-identical findings to one that never stopped — which is what
        ``tests/integration/test_checkpoint.py`` asserts by splitting a recording.
        """
        ...
