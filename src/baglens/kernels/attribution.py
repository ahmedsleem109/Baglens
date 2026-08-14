"""Stall attribution — *why* did the recorder stall, not just *when*.

The auditor says "115 topics went silent together for 2.3s". The next question is always
"because of what?", and the answer is usually somewhere else in the same recording: CPU
load, RAM pressure, supply voltage, temperature, a saturating queue.

This kernel tests candidate explanatory signals against the stall windows and ranks them
by effect size — and, critically, **reports when nothing explains them**. That last part
is not a fallback branch, it is the main result on real data: across 102 public PX4
flights and 1935 blackouts, neither CPU load nor message volume co-varies with the stalls
(see `evals/integrity/REAL_DATA.md`). A kernel that always names a cause would have
confidently named the wrong one.

Method, and why it is shaped this way:

* Compare each signal in the window immediately **before** each stall against its own
  baseline elsewhere. Sampling *inside* a stall is circular — nothing publishes there.
* Exclude every sample that falls inside any *other* stall. Stalls cluster heavily
  (index of dispersion ~5 on real flights), so a naive pre-window is full of neighbouring
  stalls and the comparison measures clustering rather than cause. This was a real error
  in the analysis that produced this kernel; the guard exists because of it.
* Report Cohen's d, not a p-value. With thousands of samples everything is "significant";
  the question is whether the difference is *large*.

**Streaming note.** Per-signal state is bounded — a Welford accumulator for the baseline,
another for the pre-windows, and a ring buffer capped at `RING` samples. Nothing here
grows with recording length, so this moves onto the live path unchanged. It is a kernel
rather than a detector only because it needs decoded field *values*, which the
payload-free arrival stream deliberately does not carry.
"""

from __future__ import annotations

import bisect
import math
from collections import deque
from dataclasses import dataclass, field

#: seconds before a stall onset that count as "leading into it"
PRE_WINDOW_S = 3.0
#: samples either side of a stall excluded from the baseline, so the comparison is
#: against genuinely quiet operation rather than the stall's own shoulders
GUARD_S = 5.0
#: ring capacity per signal — bounds state regardless of sample rate
RING = 256
#: |d| below this is noise, not an explanation
MIN_EFFECT = 0.5
#: Cohen's d is unbounded, and a perfectly separated signal divides by zero. Cap it so
#: the number stays readable and a degenerate case cannot dominate the ranking with inf.
MAX_EFFECT = 10.0
#: a signal needs at least this many pre-window samples to be worth ranking
MIN_PRE_SAMPLES = 10
MIN_BASE_SAMPLES = 50
#: Fraction of individual stalls that must shift in the *same* direction before a signal
#: counts as an explanation. An aggregate mean shift is not enough: testing a handful of
#: signals across many recordings turns up |d| > 0.5 by chance regularly, and on the
#: public PX4 corpus those chance hits pointed in contradictory directions from log to
#: log. A genuine cause precedes most stalls, not a lucky subset. 0.5 is a coin flip.
MIN_CONSISTENCY = 0.70
#: per-stall means retained per signal — bounds state while keeping the check meaningful
MAX_TRACKED_STALLS = 512


class _Welford:
    """Online mean/variance. Three floats — the whole point of the bounded budget."""

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


@dataclass
class Attribution:
    """One candidate signal's verdict on one set of stalls."""

    signal: str
    effect_size: float
    pre_mean: float
    baseline_mean: float
    n_pre: int
    n_baseline: int
    direction: str  # "elevated" | "suppressed"
    #: fraction of individual stalls whose own pre-window shifted the same way
    consistency: float = 0.0
    stalls_tested: int = 0

    @property
    def explains(self) -> bool:
        """Both tests must pass: a large aggregate shift *and* one that recurs.

        Either alone is a false-positive generator — the aggregate can be carried by a
        few outlying stalls, and a consistent but tiny shift is not worth acting on.
        """
        return (
            abs(self.effect_size) >= MIN_EFFECT
            and self.consistency >= MIN_CONSISTENCY
            and self.stalls_tested >= 3
        )

    def summary(self) -> str:
        verb = "rises to" if self.direction == "elevated" else "falls to"
        return (
            f"{self.signal} {verb} {self.pre_mean:.4g} in the {PRE_WINDOW_S:.0f}s before "
            f"stalls, against a baseline of {self.baseline_mean:.4g} "
            f"(Cohen's d = {self.effect_size:+.2f}, and it shifts that way before "
            f"{self.consistency * 100:.0f}% of the {self.stalls_tested} stalls tested)"
        )


@dataclass
class StallPattern:
    """How the stalls are distributed in time — a cause in itself.

    Clustered stalls point at a shared external resource (storage, bus, thermal);
    evenly spread ones point at something periodic; random ones at load.
    """

    count: int = 0
    total_silent_s: float = 0.0
    dispersion: float = 0.0
    kind: str = "unknown"  # "clustered" | "random" | "periodic"

    def summary(self) -> str:
        if self.kind == "clustered":
            return (
                f"{self.count} stalls arrive in bursts (index of dispersion "
                f"{self.dispersion:.1f}; 1.0 would be random). Bursty stalls point at a "
                f"shared resource — storage, a bus, or thermal throttling — rather than "
                f"at any one node"
            )
        if self.kind == "periodic":
            return (
                f"{self.count} stalls are near-evenly spaced (dispersion "
                f"{self.dispersion:.2f}), which suggests a scheduled task rather than load"
            )
        return f"{self.count} stalls with no clear temporal pattern (dispersion {self.dispersion:.1f})"


@dataclass
class AttributionReport:
    pattern: StallPattern = field(default_factory=StallPattern)
    attributions: list[Attribution] = field(default_factory=list)
    signals_tested: int = 0
    verdict: str = ""
    interpretation: str = ""

    @property
    def explained(self) -> bool:
        return any(a.explains for a in self.attributions)


class _SignalAccumulator:
    """Bounded per-signal state: two Welfords, a small ring, and per-stall sums.

    `per_stall` is capped at `MAX_TRACKED_STALLS` entries of two floats, so state stays
    bounded no matter how many stalls a recording contains.
    """

    __slots__ = ("name", "pre", "baseline", "ring", "per_stall")

    def __init__(self, name: str) -> None:
        self.name = name
        self.pre = _Welford()
        self.baseline = _Welford()
        self.ring: deque[tuple[float, float]] = deque(maxlen=RING)
        self.per_stall: dict[int, list[float]] = {}  # stall index -> [sum, count]


def _merge(windows: list[tuple[float, float]]) -> list[tuple[float, float]]:
    if not windows:
        return []
    out: list[tuple[float, float]] = []
    for lo, hi in sorted(windows):
        if out and lo <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], hi))
        else:
            out.append((lo, hi))
    return out


def _inside(t: float, windows: list[tuple[float, float]]) -> bool:
    for lo, hi in windows:
        if lo <= t <= hi:
            return True
        if lo > t:
            break
    return False


def classify_pattern(
    stalls: list[tuple[float, float]], duration_s: float, window_s: float = 30.0
) -> StallPattern:
    """Index of dispersion over fixed windows: variance/mean of stalls per window.

    1.0 is Poisson. Above 2 is clustered — which on real flight data is the norm
    (mean 5.1 across the public PX4 corpus), and is the single most informative thing
    we can say when no signal explains the stalls.
    """
    pattern = StallPattern(count=len(stalls))
    pattern.total_silent_s = sum(hi - lo for lo, hi in stalls)
    if len(stalls) < 4 or duration_s <= window_s:
        pattern.kind = "unknown"
        return pattern

    nbins = max(int(duration_s / window_s) + 1, 1)
    counts = [0] * nbins
    for lo, _ in stalls:
        idx = min(int(lo / window_s), nbins - 1)
        counts[idx] += 1

    mean = sum(counts) / nbins
    if mean <= 0:
        pattern.kind = "unknown"
        return pattern
    var = sum((c - mean) ** 2 for c in counts) / nbins
    pattern.dispersion = var / mean
    pattern.kind = (
        "clustered" if pattern.dispersion > 2.0
        else "periodic" if pattern.dispersion < 0.5
        else "random"
    )
    return pattern


class StallAttributor:
    """Feed it stall windows, then stream candidate signal samples past it.

    Single pass per signal, bounded state. `feed` is deliberately value-at-a-time so the
    same object works against a live subscription later.
    """

    def __init__(self, stalls: list[tuple[float, float]], duration_s: float) -> None:
        self.stalls = _merge(stalls)
        self.duration_s = duration_s
        self.pattern = classify_pattern(self.stalls, duration_s)
        self._starts = [lo for lo, _ in self.stalls]
        self._pre = _merge([(max(lo - PRE_WINDOW_S, 0.0), lo) for lo, _ in self.stalls])
        self._guard = _merge(
            [(max(lo - GUARD_S, 0.0), hi + GUARD_S) for lo, hi in self.stalls]
        )
        self._signals: dict[str, _SignalAccumulator] = {}

    def feed(self, signal: str, t: float, value: float) -> None:
        if not math.isfinite(value):
            return
        acc = self._signals.get(signal)
        if acc is None:
            acc = self._signals[signal] = _SignalAccumulator(signal)
        acc.ring.append((t, value))

        # A sample inside any stall says nothing about what led into one — during a
        # stall the signal is either absent or itself a victim.
        if _inside(t, self.stalls):
            return
        if _inside(t, self._pre):
            acc.pre.push(value)
            idx = self._owning_stall(t)
            if idx is not None and (
                idx in acc.per_stall or len(acc.per_stall) < MAX_TRACKED_STALLS
            ):
                slot = acc.per_stall.setdefault(idx, [0.0, 0.0])
                slot[0] += value
                slot[1] += 1.0
        elif not _inside(t, self._guard):
            acc.baseline.push(value)

    def _owning_stall(self, t: float) -> int | None:
        """Which stall's lead-in does `t` fall in? Nearest start at or after `t`."""
        i = bisect.bisect_left(self._starts, t)
        if i < len(self._starts) and self._starts[i] - PRE_WINDOW_S <= t < self._starts[i]:
            return i
        return None

    def report(self) -> AttributionReport:
        rep = AttributionReport(pattern=self.pattern, signals_tested=len(self._signals))

        for acc in self._signals.values():
            if acc.pre.n < MIN_PRE_SAMPLES or acc.baseline.n < MIN_BASE_SAMPLES:
                continue
            delta = acc.pre.mean - acc.baseline.mean
            pooled = math.sqrt((acc.pre.variance + acc.baseline.variance) / 2.0)
            if pooled <= 0:
                # Both groups are constant. If they are the *same* constant the signal
                # says nothing; if they differ, the separation is perfect — which is the
                # strongest possible evidence, not a divide-by-zero to be discarded.
                if abs(delta) <= 1e-12:
                    continue
                d = math.copysign(MAX_EFFECT, delta)
            else:
                d = max(-MAX_EFFECT, min(MAX_EFFECT, delta / pooled))
            # How many individual stalls shift the same way as the aggregate? Noise
            # lands near 0.5; a real cause lands high.
            same, tested = 0, 0
            for total, count in acc.per_stall.values():
                if count < 2:
                    continue
                tested += 1
                if math.copysign(1.0, total / count - acc.baseline.mean) == math.copysign(
                    1.0, delta
                ):
                    same += 1
            consistency = same / tested if tested else 0.0

            rep.attributions.append(
                Attribution(
                    signal=acc.name,
                    effect_size=round(d, 4),
                    pre_mean=round(acc.pre.mean, 6),
                    baseline_mean=round(acc.baseline.mean, 6),
                    n_pre=acc.pre.n,
                    n_baseline=acc.baseline.n,
                    direction="elevated" if d > 0 else "suppressed",
                    consistency=round(consistency, 3),
                    stalls_tested=tested,
                )
            )

        rep.attributions.sort(key=lambda a: -abs(a.effect_size))
        strong = [a for a in rep.attributions if a.explains]

        if strong:
            rep.verdict = "attributed"
            rep.interpretation = (
                strong[0].summary()
                + ". That is a large enough shift to be worth acting on; confirm it is "
                "causal before treating it as the fix"
            )
        elif rep.attributions:
            rep.verdict = "unexplained"
            best = rep.attributions[0]
            if abs(best.effect_size) >= MIN_EFFECT:
                # Large aggregate shift, but it does not recur — the distinction that
                # keeps a handful of chance hits from being reported as root causes.
                why = (
                    f"the strongest, {best.signal}, does shift by "
                    f"d={best.effect_size:+.2f}, but only before "
                    f"{best.consistency * 100:.0f}% of the {best.stalls_tested} stalls "
                    f"individually, which is close to chance"
                )
            else:
                why = (
                    f"the strongest, {best.signal}, moves by only "
                    f"d={best.effect_size:+.2f}"
                )
            rep.interpretation = (
                f"No candidate signal explains these stalls — {why} "
                f"({rep.signals_tested} signals tested). "
                + rep.pattern.summary()
                + ". The cause is most likely outside what this recording captured"
            )
        else:
            rep.verdict = "no_data"
            rep.interpretation = (
                "No candidate signal in this recording had enough samples to test. "
                + rep.pattern.summary()
            )
        return rep
