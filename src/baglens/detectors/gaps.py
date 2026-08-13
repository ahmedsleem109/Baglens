"""D2 — gap detection.

A gap is ``dt > k * expected_period`` with an absolute floor so 100 Hz topics do not
fire on a 50 ms hiccup. Gaps closer than 2 periods are merged. Storage is capped at
1000 gaps, largest kept, and the truncation is reported rather than hidden.

Target: recall >= 0.95, precision >= 0.90 on ``topic_dropout`` faults.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass

from ..config import CONFIG, Config
from ..models import Finding, Severity
from .cadence import TopicCadence


@dataclass
class Gap:
    topic: str
    t_start: float
    t_end: float
    expected_period: float

    @property
    def duration(self) -> float:
        return self.t_end - self.t_start

    @property
    def periods(self) -> float:
        return self.duration / self.expected_period if self.expected_period > 0 else 0.0

    def __lt__(self, other: Gap) -> bool:  # heap orders by duration, smallest first
        return self.duration < other.duration


def severity_for(gap: Gap, cfg: Config | None = None) -> Severity:
    cfg = cfg or CONFIG
    g = cfg.gap
    if gap.duration >= g.critical_absolute_s or gap.periods >= g.sev_critical:
        return Severity.CRITICAL
    if gap.periods >= g.sev_high:
        return Severity.HIGH
    if gap.periods >= g.sev_medium:
        return Severity.MEDIUM
    return Severity.LOW


class GapDetector:
    """One instance per topic. State: a bounded gap heap + 3 scalars."""

    name = "gap"

    def __init__(self, topic: str, cadence: TopicCadence, cfg: Config | None = None) -> None:
        self.topic = topic
        self.cadence = cadence
        self.cfg = cfg or CONFIG
        self.k = self.cfg.gap.k_by_sensitivity[self.cfg.sensitivity]
        self._heap: list[Gap] = []
        self._last: Gap | None = None  # open for merging
        self.total_silent = 0.0
        self.max_gap = 0.0
        self.gap_count = 0
        self.dropped_gaps = 0  # evicted by the cap

    # -- streaming ---------------------------------------------------------

    def on_arrival(self, t: float, dt: float | None) -> None:
        if dt is None or dt <= 0:
            return
        period = self.cadence.provisional_period
        if not period:
            return
        threshold = max(self.k * period, self.cfg.gap.floor_s)
        if dt <= threshold:
            return

        gap = Gap(self.topic, t - dt, t, period)
        merge_window = self.cfg.gap.merge_periods * period
        if self._last is not None and gap.t_start - self._last.t_end <= merge_window:
            # extend rather than emit: two hiccups a period apart are one event
            self.total_silent += gap.duration
            self._last.t_end = gap.t_end
            self.max_gap = max(self.max_gap, self._last.duration)
            return

        self._flush()
        self._last = gap
        self.total_silent += gap.duration
        self.max_gap = max(self.max_gap, gap.duration)

    def _flush(self) -> None:
        if self._last is None:
            return
        self.gap_count += 1
        heapq.heappush(self._heap, self._last)
        if len(self._heap) > self.cfg.gap.max_gaps:
            heapq.heappop(self._heap)  # drop the smallest; we keep the worst offenders
            self.dropped_gaps += 1
        self._last = None

    # -- results -----------------------------------------------------------

    def gaps(self) -> list[Gap]:
        self._flush()
        return sorted(self._heap, key=lambda g: g.t_start)

    def finalize(self, t_end: float) -> list[Finding]:
        out: list[Finding] = []
        period = self.cadence.expected_period or self.cadence.provisional_period or 0.0
        for gap in self.gaps():
            sev = severity_for(gap, self.cfg)
            lost = max(0, int(round(gap.duration / gap.expected_period)) - 1)
            out.append(
                Finding(
                    detector="gap",
                    severity=sev,
                    topic=self.topic,
                    t_start=gap.t_start,
                    t_end=gap.t_end,
                    summary=(
                        f"{self.topic} silent for {gap.duration:.2f}s "
                        f"({gap.periods:.0f}x its {1 / gap.expected_period:.1f} Hz period)"
                    ),
                    evidence={
                        "duration_s": round(gap.duration, 4),
                        "expected_period_s": round(gap.expected_period, 6),
                        "periods_missed": round(gap.periods, 1),
                        "estimated_lost_messages": lost,
                    },
                    confidence=min(1.0, 0.5 + gap.periods / (4 * self.k)),
                    interpretation=(
                        "the sensor or node stopped publishing, or the recorder stalled — "
                        "check health.find_gaps for co-silent topics to tell which"
                    ),
                    rule=f"dt > max({self.k} * expected_period, {self.cfg.gap.floor_s}s)",
                )
            )
        if self.dropped_gaps:
            out.append(
                Finding(
                    detector="gap",
                    severity=Severity.INFO,
                    topic=self.topic,
                    t_start=0.0,
                    t_end=t_end,
                    summary=(
                        f"{self.topic} had {self.gap_count} gaps; only the "
                        f"{self.cfg.gap.max_gaps} largest are listed"
                    ),
                    evidence={"gaps_omitted": float(self.dropped_gaps)},
                    interpretation="gap storage is bounded by design; the omitted gaps are the smallest",
                    rule=f"max_gaps={self.cfg.gap.max_gaps}",
                )
            )
        _ = period
        return out

    def state_bytes(self) -> int:
        return 48 * min(len(self._heap), self.cfg.gap.max_gaps) + 64
