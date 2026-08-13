"""D4 — jitter expansion. Also edge-relevant.

Widening timing variance precedes hard failure in control loops far more often than
a change in mean rate does. Rolling coefficient of variation over a 200-sample
window; baseline established during warmup; fire on a sustained excursion.

State: the 200-sample ring shared with the cadence estimator + 4 scalars.
Target: recall >= 0.80 on injected jitter, precision >= 0.85.
"""

from __future__ import annotations

from ..config import CONFIG, Config
from ..models import Finding, Severity
from .base import RollingWelford
from .cadence import TopicCadence


class JitterDetector:
    name = "jitter"

    def __init__(self, topic: str, cadence: TopicCadence, cfg: Config | None = None) -> None:
        self.topic = topic
        self.cadence = cadence
        self.cfg = cfg or CONFIG
        j = self.cfg.jitter
        # Share the cadence estimator's rolling window rather than keeping a second
        # copy of the same statistic — on a 40-topic robot that duplication was the
        # single largest line in the per-topic state budget.
        self.window = cadence.cv_window if cadence.cv_window.window == j.window else RollingWelford(j.window)
        self.multiplier = j.cv_multiplier_by_sensitivity[self.cfg.sensitivity]
        self.floor = j.cv_floor_by_sensitivity[self.cfg.sensitivity]
        self._episode_start: float | None = None
        self._peak_cv = 0.0
        self._findings: list[Finding] = []
        self.max_cv = 0.0

    def on_arrival(self, t: float, dt: float | None) -> None:
        if dt is None or dt <= 0:
            return
        period = self.cadence.provisional_period
        # a gap is a gap, not jitter — D2 owns it, and letting it through here
        # would make every dropout look like a timing problem too
        if period and dt > 5.0 * period:
            return
        if self.window is not self.cadence.cv_window:
            self.window.push(dt)
        if self.window.n < max(20, self.cfg.jitter.window // 4):
            return
        cv = self.window.cv
        self.max_cv = max(self.max_cv, cv)
        baseline = self.cadence.cv_baseline
        if baseline is None:
            return
        threshold = max(self.multiplier * baseline, self.floor)

        if cv > threshold:
            if self._episode_start is None:
                self._episode_start = t
                self._peak_cv = cv
            else:
                self._peak_cv = max(self._peak_cv, cv)
        elif self._episode_start is not None:
            self._close(t, threshold)

    def _close(self, t: float, threshold: float) -> None:
        start = self._episode_start
        self._episode_start = None
        if start is None:
            return
        if t - start < self.cfg.jitter.sustain_s:
            return  # not sustained; a single noisy stretch is not a finding
        baseline = self.cadence.cv_baseline or 0.0
        ratio = self._peak_cv / baseline if baseline > 0 else float("inf")
        sev = Severity.HIGH if self._peak_cv > 3 * threshold else Severity.MEDIUM
        self._findings.append(
            Finding(
                detector="jitter",
                severity=sev,
                topic=self.topic,
                t_start=start,
                t_end=t,
                summary=(
                    f"{self.topic} timing variance expanded to CV={self._peak_cv:.2f} "
                    f"(baseline {baseline:.2f}) for {t - start:.0f}s"
                ),
                evidence={
                    "peak_cv": round(self._peak_cv, 4),
                    "baseline_cv": round(baseline, 4),
                    "threshold": round(threshold, 4),
                    "ratio": round(min(ratio, 999.0), 2),
                    "duration_s": round(t - start, 2),
                },
                confidence=min(1.0, self._peak_cv / max(threshold, 1e-6) / 3),
                interpretation=(
                    "the topic still arrives at roughly the right average rate but the "
                    "spacing has become irregular — scheduler contention, CPU pressure, or "
                    "a driver buffering. Control loops degrade here before rates change"
                ),
                rule=(
                    f"rolling_cv(window={self.cfg.jitter.window}) > "
                    f"max({self.multiplier}*cv_baseline, {self.floor}) "
                    f"sustained >= {self.cfg.jitter.sustain_s}s"
                ),
            )
        )

    def finalize(self, t_end: float) -> list[Finding]:
        if self._episode_start is not None:
            baseline = self.cadence.cv_baseline or 0.0
            self._close(t_end, max(self.multiplier * baseline, self.floor))
        return self._findings

    def state_bytes(self) -> int:
        # zero when the variance window is shared with the cadence estimator, which
        # already counts it
        own = 0 if self.window is self.cadence.cv_window else 8 * self.cfg.jitter.window
        return own + 96
