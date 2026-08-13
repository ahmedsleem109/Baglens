"""D5 — dropped-message estimation.

Two independent estimators, both reported, plus their disagreement. Low agreement
is itself informative: it usually means drops were diffuse (recorder backpressure)
rather than clustered (sensor outage), and the interpretation says so.

``active_duration`` excludes CRITICAL gaps, so a topic that simply stopped is not
reported as having dropped ten thousand messages.
"""

from __future__ import annotations

from ..config import CONFIG, Config
from ..models import Finding, Severity
from .cadence import TopicCadence
from .gaps import GapDetector, severity_for


class DroppedEstimator:
    name = "dropped"

    def __init__(
        self,
        topic: str,
        cadence: TopicCadence,
        gap: GapDetector | None,
        cfg: Config | None = None,
    ) -> None:
        self.topic = topic
        self.cadence = cadence
        self.gap = gap
        self.cfg = cfg or CONFIG
        self._last: tuple[int, float] | None = None
        self._parts: dict[str, float] = {}

    def estimate(self, t_end: float) -> tuple[int, float]:
        cad = self.cadence
        hz = cad.expected_hz
        if not hz or cad.first_t is None or cad.count < 2:
            return 0, 0.0

        critical_silent = 0.0
        gap_based = 0
        if self.gap is not None:
            for g in self.gap.gaps():
                if severity_for(g, self.cfg) >= Severity.CRITICAL:
                    critical_silent += g.duration
                else:
                    gap_based += max(0, int(round(g.duration * hz)) - 1)

        active = max(t_end - cad.first_t - critical_silent, 1e-9)
        count_based = max(0, int(round(hz * active)) - cad.count)

        denom = max(count_based, gap_based, 1)
        confidence = 1.0 - abs(count_based - gap_based) / denom
        self._parts = {
            "count_based": float(count_based),
            "gap_based": float(gap_based),
            "expected_hz": round(hz, 4),
            "active_duration_s": round(active, 3),
            "observed_count": float(cad.count),
            "excluded_silent_s": round(critical_silent, 3),
        }
        dropped = max(count_based, gap_based)
        expected_total = hz * active
        rate = dropped / expected_total if expected_total > 0 else 0.0

        # Noise floor. expected_hz is itself an estimate from ~50 warmup samples and
        # carries a relative error around cv/sqrt(n). Reporting a drop rate smaller than
        # the uncertainty in the rate that produced it is how a detector loses trust on
        # clean data — the failure mode that matters most.
        cv = self.cadence.period_cv or 0.02
        floor = max(0.02, 4.0 * cv / (self.cfg.cadence.ring_size**0.5))
        self._parts["noise_floor"] = round(floor, 4)
        self._parts["drop_rate"] = round(rate, 4)
        if rate < floor or dropped < 5:
            dropped = 0

        self._last = (dropped, max(0.0, confidence))
        return self._last

    def finalize(self, t_end: float) -> list[Finding]:
        if self._last is None:
            self.estimate(t_end)
        if self._last is None:
            return []
        dropped, confidence = self._last
        if dropped <= 0:
            return []
        rate = self._parts.get("drop_rate", 0.0)
        sev = (
            Severity.CRITICAL
            if rate >= 0.30
            else Severity.HIGH
            if rate >= 0.10
            else Severity.MEDIUM
            if rate >= 0.03
            else Severity.LOW
        )
        clustered = confidence >= 0.5
        interp = (
            "drops are clustered — consistent with a sensor or node outage; the gap list "
            "shows exactly when"
            if clustered
            else "the two estimators disagree, which usually means the drops were diffuse "
            "rather than clustered — the classic signature of recorder backpressure "
            "(check the recorder-lag curve in health.clock_report)"
        )
        return [
            Finding(
                detector="dropped",
                severity=sev,
                topic=self.topic,
                t_start=self.cadence.first_t or 0.0,
                t_end=t_end,
                summary=(
                    f"{self.topic} is missing ~{dropped} messages "
                    f"({rate * 100:.1f}% of expected)"
                ),
                evidence={**self._parts, "estimated_dropped": float(dropped),
                          "agreement": round(confidence, 3)},
                confidence=max(0.3, confidence),
                interpretation=interp,
                rule=(
                    "count_based = expected_hz*active_duration - observed_count; "
                    "gap_based = sum(round(gap*hz)-1); active excludes CRITICAL gaps"
                ),
            )
        ]

    def state_bytes(self) -> int:
        return 96
