"""D3 — rate degradation (streaming trend). The edge-relevant one.

A topic that silently slows from 30 Hz to 22 Hz over ten minutes is a classic
precursor: a node falling behind, thermal throttling, a growing queue. Nothing
detects it today because nothing looks at rate as a *trend*.

EWMA of inter-arrival (alpha=0.02, ~50-sample memory) sampled into 10 s buckets;
Theil-Sen slope over the last 30 buckets, gated on Kendall's tau. Theil-Sen because
least squares gets dragged by a single gap, and this detector must survive the very
faults D2 is reporting.

State: 30 floats + 2 EWMA scalars. ~300 bytes.
Target: detect a 30%-over-10-minutes degradation with recall >= 0.85.
"""

from __future__ import annotations

from collections import deque
from typing import Any

from ..config import CONFIG, Config
from ..models import Finding, Severity
from .base import Ewma, kendall_tau_p, theil_sen
from .cadence import TopicCadence


class RateDegradationDetector:
    name = "rate_degradation"

    def __init__(self, topic: str, cadence: TopicCadence, cfg: Config | None = None) -> None:
        self.topic = topic
        self.cadence = cadence
        self.cfg = cfg or CONFIG
        d = self.cfg.degradation
        self.ewma = Ewma(d.ewma_alpha)
        # Bucket centres are uniformly spaced, so storing them would be storing an
        # arithmetic sequence. Keep the index of the oldest retained bucket instead.
        self.bucket_hz: deque[float] = deque(maxlen=d.n_buckets)
        self._bucket_index = 0  # index of the next bucket to be closed
        self._t_origin: float | None = None
        self._bucket_end: float | None = None
        self._episode: tuple[float, float, float] | None = None  # (t_start, rel_slope, tau)
        self._findings: list[Finding] = []
        self.threshold = d.rel_slope_by_sensitivity[self.cfg.sensitivity]

    # -- streaming ---------------------------------------------------------

    def on_arrival(self, t: float, dt: float | None) -> None:
        if dt is None or dt <= 0:
            return
        d = self.cfg.degradation
        # a gap must not be read as "the rate dropped to 0.1 Hz for one sample"
        period = self.cadence.provisional_period
        if period and dt > 5.0 * period:
            return
        self.ewma.push(dt)

        if self._bucket_end is None:
            self._bucket_end = t + d.bucket_s
            self._t_origin = t + d.bucket_s / 2
            return
        while t >= self._bucket_end:
            if self.ewma.value > 0:
                self.bucket_hz.append(1.0 / self.ewma.value)
                self._bucket_index += 1
            self._bucket_end += d.bucket_s
            self._evaluate()

    def _bucket_times(self) -> list[float]:
        d = self.cfg.degradation
        origin = self._t_origin or 0.0
        first = self._bucket_index - len(self.bucket_hz)
        return [origin + (first + i) * d.bucket_s for i in range(len(self.bucket_hz))]

    def _evaluate(self) -> None:
        d = self.cfg.degradation
        if len(self.bucket_hz) < d.min_buckets:
            return
        xs = self._bucket_times()
        ys = list(self.bucket_hz)
        slope = theil_sen(xs, ys)  # Hz per second
        tau, p = kendall_tau_p(xs, ys)
        window = xs[-1] - xs[0]
        baseline = self.cadence.expected_hz or (sum(ys) / len(ys))
        rel = slope * window / baseline if baseline else 0.0

        firing = abs(rel) > self.threshold and p < d.tau_p_max
        if firing and self._episode is None:
            self._episode = (xs[0], rel, tau)
        elif firing:
            start, prev_rel, prev_tau = self._episode  # type: ignore[misc]
            self._episode = (start, rel if abs(rel) > abs(prev_rel) else prev_rel, tau)
        elif self._episode is not None:
            self._close(xs[-1])

    def _close(self, t_end: float) -> None:
        if self._episode is None:
            return
        t_start, rel, tau = self._episode
        self._episode = None
        direction = "slowed" if rel < 0 else "sped up"
        pct = abs(rel) * 100.0
        sev = (
            Severity.HIGH
            if pct >= 40
            else Severity.MEDIUM
            if pct >= 20
            else Severity.LOW
        )
        hz0 = self.bucket_hz[0] if self.bucket_hz else 0.0
        hz1 = self.bucket_hz[-1] if self.bucket_hz else 0.0
        self._findings.append(
            Finding(
                detector="rate_degradation",
                severity=sev,
                topic=self.topic,
                t_start=t_start,
                t_end=t_end,
                summary=(
                    f"{self.topic} {direction} by {pct:.0f}% over "
                    f"{t_end - t_start:.0f}s ({hz0:.1f} → {hz1:.1f} Hz)"
                ),
                evidence={
                    "relative_slope": round(rel, 4),
                    "kendall_tau": round(tau, 3),
                    "hz_first_bucket": round(hz0, 3),
                    "hz_last_bucket": round(hz1, 3),
                    "buckets": float(len(self.bucket_hz)),
                },
                confidence=min(1.0, abs(tau)),
                interpretation=(
                    "a sustained rate trend, not noise — a node falling behind, thermal "
                    "throttling, or a growing queue. This is the shape that precedes a "
                    "hard failure; it will not show up in an average-rate report"
                ),
                rule=(
                    f"|theil_sen_slope * window / expected_hz| > {self.threshold} "
                    f"AND kendall_tau p < {self.cfg.degradation.tau_p_max}"
                ),
            )
        )

    def finalize(self, t_end: float) -> list[Finding]:
        self._close(t_end)
        return self._findings

    def state_bytes(self) -> int:
        return 8 * self.cfg.degradation.n_buckets + 96

    # -- checkpoint --------------------------------------------------------

    def to_state(self) -> dict[str, Any]:
        return {
            "topic": self.topic,
            "ewma": self.ewma.to_state(),
            "bucket_hz": list(self.bucket_hz),
            "bucket_index": self._bucket_index,
            "t_origin": self._t_origin,
            "bucket_end": self._bucket_end,
            "episode": list(self._episode) if self._episode is not None else None,
            "findings": [f.model_dump(mode="json") for f in self._findings],
        }

    @classmethod
    def from_state(
        cls, state: dict[str, Any], cadence: TopicCadence, cfg: Config | None = None
    ) -> RateDegradationDetector:
        obj = cls(str(state["topic"]), cadence, cfg)
        obj.ewma = Ewma.from_state(state["ewma"])
        obj.bucket_hz = deque(
            (float(x) for x in state["bucket_hz"]), maxlen=obj.cfg.degradation.n_buckets
        )
        obj._bucket_index = int(state["bucket_index"])
        obj._t_origin = state["t_origin"]
        obj._bucket_end = state["bucket_end"]
        ep = state["episode"]
        obj._episode = (float(ep[0]), float(ep[1]), float(ep[2])) if ep is not None else None
        obj._findings = [Finding.model_validate(f) for f in state["findings"]]
        return obj
