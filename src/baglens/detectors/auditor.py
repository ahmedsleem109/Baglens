"""The single-pass auditor.

One iteration over the arrival stream feeds every detector. Nothing is buffered,
nothing is revisited, and the whole thing is restartable from a checkpoint because
each detector's state is a fixed-size struct.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from ..config import CONFIG, Config
from ..models import (
    ClockReport,
    FileIntegrity,
    Finding,
    HealthReport,
    Severity,
    TopicHealth,
)
from ..provenance import Provenance, mission_id_for
from ..readers.base import BagReader
from .cadence import TopicCadence
from .gaps import Gap, GapDetector

ALL_DETECTORS = (
    "cadence",
    "gap",
    "rate_degradation",
    "jitter",
    "dropped",
    "clock",
    "correlation",
    "file_integrity",
)


@dataclass
class TopicState:
    """Everything the auditor keeps for one topic. Asserted under 2 KB."""

    cadence: TopicCadence
    gap: GapDetector | None = None
    degradation: Any = None
    jitter: Any = None
    msg_type: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def state_bytes(self) -> int:
        total = self.cadence.state_bytes()
        for d in (self.gap, self.degradation, self.jitter):
            if d is not None:
                total += d.state_bytes()
        return total


class Auditor:
    """Run the detector library over one recording in a single pass."""

    def __init__(
        self,
        reader: BagReader,
        cfg: Config | None = None,
        detectors: list[str] | None = None,
        topics: list[str] | None = None,
    ) -> None:
        self.reader = reader
        self.cfg = cfg or CONFIG
        self.enabled = set(detectors or ALL_DETECTORS)
        self.topic_filter = topics
        self.meta = reader.metadata()
        self.states: dict[str, TopicState] = {}
        self.t0: float | None = None
        self.t_end: float = 0.0
        self.n = 0
        self.clock: Any = None
        self.correlation: Any = None
        self.integrity: FileIntegrity | None = None
        from .timeline import TimelineAccumulator

        self.timeline = TimelineAccumulator()

    # -- the pass ----------------------------------------------------------

    def _state_for(self, topic: str) -> TopicState:
        st = self.states.get(topic)
        if st is not None:
            return st
        info = self.meta.topic(topic)
        cad = TopicCadence(
            topic,
            self.cfg.cadence,
            declared_period_s=info.declared_period_s if info else None,
            cv_window_size=self.cfg.jitter.window,
        )
        st = TopicState(cadence=cad, msg_type=info.msg_type if info else "")
        if "gap" in self.enabled:
            st.gap = GapDetector(topic, cad, self.cfg)
        if "rate_degradation" in self.enabled:
            from .degradation import RateDegradationDetector

            st.degradation = RateDegradationDetector(topic, cad, self.cfg)
        if "jitter" in self.enabled:
            from .jitter import JitterDetector

            st.jitter = JitterDetector(topic, cad, self.cfg)
        self.states[topic] = st
        return st

    def run(self) -> HealthReport:
        if "clock" in self.enabled:
            from .clock import ClockDetector

            self.clock = ClockDetector(self.cfg)
        if "correlation" in self.enabled:
            from .correlation import CorrelationDetector

            self.correlation = CorrelationDetector(self.cfg)

        for arrival in self.reader.arrivals(self.topic_filter):
            log_t_ns, pub_t_ns = arrival.log_time_ns, arrival.publish_time_ns
            if self.t0 is None:
                self.t0 = log_t_ns / 1e9
            t = log_t_ns / 1e9 - self.t0
            pub_t = pub_t_ns / 1e9 - self.t0
            self.n += 1
            self.t_end = max(self.t_end, t)

            st = self._state_for(arrival.topic)
            dt = st.cadence.push(t, arrival.size_bytes)
            self.timeline.push(arrival.topic, t)

            if st.gap is not None:
                st.gap.on_arrival(t, dt)
            if st.degradation is not None:
                st.degradation.on_arrival(t, dt)
            if st.jitter is not None:
                st.jitter.on_arrival(t, dt)
            if self.clock is not None:
                self.clock.on_arrival(arrival.topic, t, pub_t)
            if self.correlation is not None:
                self.correlation.on_arrival(arrival.topic, t, dt, st.cadence.provisional_period)

        if "file_integrity" in self.enabled:
            from ..readers.recovery import validate_file

            self.integrity = validate_file(self.reader.path)

        return self._assemble()

    # -- assembly ----------------------------------------------------------

    def _finding_id(self, f: Finding) -> str:
        raw = f"{f.detector}|{f.topic}|{f.t_start:.3f}|{f.t_end:.3f}"
        return hashlib.blake2b(raw.encode(), digest_size=4).hexdigest()

    def all_gaps(self) -> list[Gap]:
        out: list[Gap] = []
        for st in self.states.values():
            if st.gap is not None:
                out.extend(st.gap.gaps())
        out.sort(key=lambda g: g.t_start)
        return out

    def _assemble(self) -> HealthReport:
        from .score import build_caveats, file_score, overall_score, topic_score, verdict_for

        mission_id = ""
        try:
            mission_id = mission_id_for(self.reader.path)
        except Exception:
            mission_id = self.meta.path

        prov = Provenance(
            mission_id=mission_id,
            path=str(self.reader.path),
            topics=sorted(self.states),
            time_range=(0.0, self.t_end),
            method="single_pass_auditor(" + ",".join(sorted(self.enabled)) + ")",
            sample_count=self.n,
            partial=self.meta.partial,
            warnings=list(self.meta.warnings),
        )

        findings: list[Finding] = []
        topics: list[TopicHealth] = []

        for topic, st in sorted(self.states.items()):
            cad = st.cadence
            silent = st.gap.total_silent if st.gap else 0.0
            th = TopicHealth(
                topic=topic,
                msg_type=st.msg_type,
                count=cad.count,
                expected_hz=cad.expected_hz,
                observed_hz=cad.observed_hz(self.t_end, silent),
                hz_source=cad.hz_source,  # type: ignore[arg-type]
                jitter_cv=round(cad.jitter_cv, 4),
                gap_count=st.gap.gap_count if st.gap else 0,
                max_gap_s=round(st.gap.max_gap, 4) if st.gap else 0.0,
                total_silent_s=round(silent, 4),
            )
            if st.gap is not None:
                findings += st.gap.finalize(self.t_end)
            if st.degradation is not None:
                findings += st.degradation.finalize(self.t_end)
            if st.jitter is not None:
                findings += st.jitter.finalize(self.t_end)
            if cad.qos_mismatch and cad.declared_period:
                findings.append(
                    Finding(
                        detector="qos_mismatch",
                        severity=Severity.MEDIUM,
                        topic=topic,
                        t_start=0.0,
                        t_end=self.t_end,
                        summary=(
                            f"{topic} declares a {1 / cad.declared_period:.1f} Hz QoS deadline "
                            f"but publishes at {cad.expected_hz or 0.0:.1f} Hz"
                        ),
                        evidence={
                            "declared_hz": round(1 / cad.declared_period, 3),
                            "observed_hz": round(cad.expected_hz or 0.0, 3),
                        },
                        interpretation=(
                            "the recorded QoS profile does not describe this topic's actual "
                            "behaviour; subscribers relying on that deadline will see missed-"
                            "deadline events, and thresholds derived from it would be wrong. "
                            "The observed rate is used instead"
                        ),
                        rule="declared_period outside [observed/3, observed*3]",
                    )
                )
            if "dropped" in self.enabled:
                from .dropped import DroppedEstimator

                est = DroppedEstimator(topic, cad, st.gap, self.cfg)
                th.estimated_dropped, th.dropped_confidence = est.estimate(self.t_end)
                findings += est.finalize(self.t_end)
            th.score = topic_score(th, findings, self.cfg, duration_s=self.t_end)
            topics.append(th)

        clock_report: ClockReport | None = None
        if self.clock is not None:
            clock_report = self.clock.report(prov)
            findings += self.clock.finalize(self.t_end)

        if self.correlation is not None:
            self.correlation.classify(self.all_gaps())
            findings += self.correlation.finalize(self.t_end)

        if self.integrity is not None:
            findings += _integrity_findings(self.integrity, self.t_end, self.t0 or 0.0)

        for f in findings:
            f.id = self._finding_id(f)
            if not f.provenance.path:
                f.provenance = Provenance(
                    mission_id=mission_id,
                    path=str(self.reader.path),
                    topics=[f.topic] if f.topic else [],
                    time_range=(f.t_start, f.t_end),
                    method=f.rule or f.detector,
                    sample_count=self.n,
                    partial=self.meta.partial,
                )
        findings.sort(key=lambda f: (-int(f.severity), f.t_start))

        fscore = file_score(self.integrity)
        overall = overall_score(topics, fscore, self.cfg)
        verdict = verdict_for(overall, fscore, self.cfg)

        return HealthReport(
            mission_id=mission_id,
            path=str(self.reader.path),
            duration_s=round(self.t_end, 3),
            overall_score=round(overall, 1),
            verdict=verdict,  # type: ignore[arg-type]
            findings=findings,
            topics=topics,
            file_integrity=self.integrity,
            clock=clock_report,
            caveats=build_caveats(findings, topics, self.integrity),
            provenance=prov,
        )


def _integrity_findings(fi: FileIntegrity, t_end: float, t0_epoch: float = 0.0) -> list[Finding]:
    """``FileIntegrity.last_readable_time`` is an epoch timestamp — the validator runs
    standalone and has no notion of a bag-relative clock. Findings are always in seconds
    from the start of the recording, so convert here rather than leaking epoch seconds
    into a timeline that is otherwise relative."""
    out: list[Finding] = []
    last_rel = 0.0
    if fi.last_readable_time is not None:
        last_rel = min(max(fi.last_readable_time - t0_epoch, 0.0), t_end)
    if not fi.readable:
        out.append(
            Finding(
                detector="file_integrity",
                severity=Severity.CRITICAL,
                t_start=0.0,
                t_end=t_end,
                summary=f"file is not readable: {'; '.join(fi.notes) or 'unknown structural failure'}",
                interpretation="no conclusion can be drawn from this recording",
                rule="structural_validation",
            )
        )
        return out
    if fi.truncated_bytes:
        out.append(
            Finding(
                detector="file_integrity",
                severity=Severity.HIGH,
                t_start=last_rel,
                t_end=t_end,
                summary=(
                    f"file truncated — ~{fi.truncated_bytes} bytes at the tail are unreadable; "
                    f"data ends at t={last_rel:.1f}s"
                ),
                evidence={
                    "truncated_bytes": float(fi.truncated_bytes),
                    "size_bytes": float(fi.size_bytes),
                },
                interpretation=(
                    "the recorder was killed or the disk filled; everything after the last "
                    "readable timestamp is simply absent, not zero"
                ),
                rule="sequential_recovery_scan",
            )
        )
    elif not fi.has_summary:
        out.append(
            Finding(
                detector="file_integrity",
                severity=Severity.MEDIUM,
                t_start=0.0,
                t_end=t_end,
                summary="no MCAP summary section — file recovered by sequential scan",
                interpretation=(
                    "recording is in progress or the writer never closed the file; "
                    "counts are what was readable, not what was recorded"
                ),
                rule="summary_section_present",
            )
        )
    for issue in fi.chunk_issues:
        if issue.kind == "crc_mismatch":
            out.append(
                Finding(
                    detector="file_integrity",
                    severity=Severity.HIGH,
                    t_start=issue.t_start or 0.0,
                    t_end=issue.t_end or t_end,
                    summary=f"CRC mismatch in chunk at offset {issue.offset}",
                    interpretation="that time range is corrupt; treat its contents as untrustworthy",
                    rule="chunk_crc",
                )
            )
    return out
