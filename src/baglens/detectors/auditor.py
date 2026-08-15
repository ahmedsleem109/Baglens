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
from .timeline import TimelineAccumulator

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
        self.timeline = TimelineAccumulator()
        #: topic -> why it has no cadence to measure ("sparse" | "aperiodic"), filled in
        #: by `_assemble`. The audit already drops these topics' rate-derived findings;
        #: `health.find_gaps` reads this so it can make the same call.
        self.unassessable: dict[str, str] = {}

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

    def _ensure_global_detectors(self) -> None:
        """Cross-topic detectors, created once. Guarded so a restored auditor keeps the
        state it was given instead of starting them over."""
        if "clock" in self.enabled and self.clock is None:
            from .clock import ClockDetector

            self.clock = ClockDetector(self.cfg)
        if "correlation" in self.enabled and self.correlation is None:
            from .correlation import CorrelationDetector

            self.correlation = CorrelationDetector(
                self.cfg, expected_topics=self._expected_topic_count()
            )

    def _expected_topic_count(self) -> int:
        """How many topics this recording should carry — the correlation denominator.

        Only topics the reader says carry messages count: a channel declared in the file
        but never published can never be co-silent, and including it would permanently
        depress concurrency. Readers that do not populate per-topic counts fall back to
        the declared list, and a source that cannot enumerate topics at all returns 0,
        which leaves the detector scoring against the topics it has seen.
        """
        topics = self.meta.topics
        if self.topic_filter is not None:
            wanted = set(self.topic_filter)
            topics = [t for t in topics if t.topic in wanted]
        with_messages = sum(1 for t in topics if t.count > 0)
        return with_messages or len(topics)

    def push(self, arrival: Any) -> None:
        """Feed one arrival to every detector. The whole pass is this, in a loop."""
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

    def finish(self) -> HealthReport:
        """Close every detector and assemble the report. Safe to call on a restored
        auditor: nothing here needs an arrival this process did not see."""
        if "file_integrity" in self.enabled:
            from ..readers.recovery import validate_file

            self.integrity = validate_file(self.reader.path)

        return self._assemble()

    def run(self) -> HealthReport:
        self._ensure_global_detectors()
        for arrival in self.reader.arrivals(self.topic_filter):
            self.push(arrival)
        return self.finish()

    # -- checkpoint --------------------------------------------------------

    def to_state(self) -> dict[str, Any]:
        """The complete auditor as JSON-safe data.

        This is what makes the streaming constraint pay: an audit interrupted at any
        arrival resumes from here and reaches the same findings as one that ran
        straight through. ``t0`` travels with it because every timestamp downstream is
        relative to it — a resumed pass that re-derived ``t0`` from its first arrival
        would silently shift the whole second half of the timeline.
        """
        self._ensure_global_detectors()
        return {
            "version": 1,
            "t0": self.t0,
            "t_end": self.t_end,
            "n": self.n,
            "enabled": sorted(self.enabled),
            "topic_filter": self.topic_filter,
            "timeline": self.timeline.to_state(),
            "clock": self.clock.to_state() if self.clock is not None else None,
            "correlation": self.correlation.to_state() if self.correlation is not None else None,
            "topics": {
                topic: {
                    "msg_type": st.msg_type,
                    "cadence": st.cadence.to_state(),
                    "gap": st.gap.to_state() if st.gap is not None else None,
                    "degradation": st.degradation.to_state() if st.degradation is not None else None,
                    "jitter": st.jitter.to_state() if st.jitter is not None else None,
                }
                for topic, st in self.states.items()
            },
        }

    @classmethod
    def from_state(
        cls, state: dict[str, Any], reader: BagReader, cfg: Config | None = None
    ) -> Auditor:
        version = int(state.get("version", 0))
        if version != 1:
            raise ValueError(f"unsupported auditor checkpoint version {version!r}")

        obj = cls(
            reader,
            cfg,
            detectors=list(state["enabled"]),
            topics=state["topic_filter"],
        )
        obj.t0 = state["t0"]
        obj.t_end = float(state["t_end"])
        obj.n = int(state["n"])
        obj.timeline = TimelineAccumulator.from_state(state["timeline"])

        if state["clock"] is not None:
            from .clock import ClockDetector

            obj.clock = ClockDetector.from_state(state["clock"], obj.cfg)
        if state["correlation"] is not None:
            from .correlation import CorrelationDetector

            obj.correlation = CorrelationDetector.from_state(state["correlation"], obj.cfg)

        for topic, ts in state["topics"].items():
            cadence = TopicCadence.from_state(ts["cadence"], obj.cfg.cadence)
            st = TopicState(cadence=cadence, msg_type=str(ts["msg_type"]))
            if ts["gap"] is not None:
                st.gap = GapDetector.from_state(ts["gap"], cadence, obj.cfg)
            if ts["degradation"] is not None:
                from .degradation import RateDegradationDetector

                st.degradation = RateDegradationDetector.from_state(
                    ts["degradation"], cadence, obj.cfg
                )
            if ts["jitter"] is not None:
                from .jitter import JitterDetector

                st.jitter = JitterDetector.from_state(ts["jitter"], cadence, obj.cfg)
            obj.states[topic] = st
        return obj

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
        self.unassessable.clear()  # rebuilt below; `_assemble` must stay idempotent

        # System-wide stalls are resolved *first*, because everything below has to know
        # which silences were the recorder's fault rather than the topic's. Scoring a
        # topic for a stall it merely sat through — and then billing it for the messages
        # it "lost" — is what made every real recording read as compromised.
        stall_windows: list[tuple[float, float]] = []
        if self.correlation is not None:
            self.correlation.classify(self.all_gaps())
            stall_windows = [(s.start, s.end) for s in self.correlation.merged_stalls()]

        for topic, st in sorted(self.states.items()):
            cad = st.cadence
            silent = st.gap.total_silent if st.gap else 0.0
            stall_silent = (
                _overlap_seconds(st.gap.gaps(), stall_windows) if st.gap else 0.0
            )
            reason = _unassessable_reason(cad, self.cfg)
            if reason is not None:
                self.unassessable[topic] = reason
                # Every rate-based check downstream is derived from a period this topic
                # never actually held, so none of their findings mean anything here.
                findings.append(_aperiodic_finding(topic, cad, self.t_end, reason))
                topics.append(_aperiodic_health(topic, st, cad, silent, stall_silent))
                continue
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
                stall_silent_s=round(stall_silent, 4),
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

                est = DroppedEstimator(topic, cad, st.gap, self.cfg, stall_silent_s=stall_silent)
                th.estimated_dropped, th.dropped_confidence = est.estimate(self.t_end)
                findings += est.finalize(self.t_end)
            th.score = topic_score(th, findings, self.cfg, duration_s=self.t_end)
            topics.append(th)

        clock_report: ClockReport | None = None
        if self.clock is not None:
            clock_report = self.clock.report(prov)
            findings += self.clock.finalize(self.t_end)

        if self.correlation is not None:
            findings += self.correlation.finalize(self.t_end)

        if self.integrity is not None:
            findings += _integrity_findings(self.integrity, self.t_end, self.t0 or 0.0)

        findings = _roll_up_stall_gaps(findings, stall_windows)

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
        overall = overall_score(topics, fscore, self.cfg, duration_s=self.t_end)
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


def _sustained_hz(cad: TopicCadence) -> float:
    """The rate this topic actually held across its own lifetime.

    Measured between its first and last message rather than over the whole recording, so
    a topic that starts late or dies early is judged on the span it was alive for.
    """
    if cad.first_t is None or cad.last_t is None or cad.count < 2:
        return 0.0
    span = cad.last_t - cad.first_t
    return cad.count / span if span > 0 else 0.0


def _unassessable_reason(cad: TopicCadence, cfg: Config) -> str | None:
    """Why this topic cannot support a rate model, or None if it can.

    Two ways a topic has no cadence to measure, and both were producing findings:

    * **sparse** — warmup never completed, so no baseline was ever established. The
      auditor still hands out a `provisional_period` during warmup so that a topic dying
      early is not missed, but for a topic that publishes five times in the whole
      recording that provisional value is a median over a couple of samples. On real
      flights `/home_position` (5 messages) and `/vehicle_command_ack` (7) were being
      scored on it, and one of them then set `min()` for the entire report.
    * **aperiodic** — warmup completed, but the learned rate is far above the rate the
      topic actually sustained. Event-driven topics arrive in bursts, so the modal
      inter-arrival is the spacing *inside* a burst.

    Either way the honest answer is that we cannot assess the topic, not that it failed.
    """
    if not cad.ready:
        return "sparse"
    expected = cad.expected_hz
    sustained = _sustained_hz(cad)
    if not expected or sustained <= 0:
        return None
    if expected > cfg.cadence.aperiodic_ratio * sustained:
        return "aperiodic"
    return None


def _aperiodic_finding(topic: str, cad: TopicCadence, t_end: float, reason: str) -> Finding:
    sustained = _sustained_hz(cad)
    span = (cad.last_t or 0.0) - (cad.first_t or 0.0)
    if reason == "sparse":
        summary = (
            f"{topic} published {cad.count} times in {span:.1f}s — too few to establish "
            f"a rate"
        )
        interpretation = (
            "no baseline was ever established for this topic, so rate, gap and "
            "dropped-message checks are skipped. Its silences are not evidence of loss: "
            "with this few messages there is no way to tell an event-driven topic from a "
            "failing one, and claiming either would be a guess"
        )
        rule = f"warmup never completed ({cad.count} < {CONFIG.cadence.warmup_messages} messages)"
    else:
        summary = (
            f"{topic} has no stable publication rate — {cad.count} messages over "
            f"{span:.1f}s, arriving in bursts"
        )
        interpretation = (
            "this is an event-driven topic, not a broken one: it publishes when "
            "something happens rather than on a schedule. Rate, gap and dropped-message "
            "checks are skipped for it because a period learned from burst spacing would "
            "describe behaviour it never had. Its silences are not evidence of loss"
        )
        rule = f"modal_hz > {CONFIG.cadence.aperiodic_ratio} * (count / active_span)"

    return Finding(
        detector="aperiodic",
        severity=Severity.INFO,
        topic=topic,
        t_start=cad.first_t or 0.0,
        t_end=t_end,
        summary=summary,
        evidence={
            "message_count": float(cad.count),
            "sustained_hz": round(sustained, 4),
            "modal_hz": round(cad.expected_hz or 0.0, 4),
        },
        confidence=0.9,
        interpretation=interpretation,
        rule=rule,
    )


def _aperiodic_health(
    topic: str, st: TopicState, cad: TopicCadence, silent: float, stall_silent: float
) -> TopicHealth:
    """Report the topic honestly without a rate model, and without a score.

    Scoring it 100 would claim it is healthy, which we cannot know. It is excluded from
    the overall score instead, and `hz_source="aperiodic"` says why.
    """
    return TopicHealth(
        topic=topic,
        msg_type=st.msg_type,
        count=cad.count,
        expected_hz=None,
        observed_hz=round(_sustained_hz(cad), 4),
        hz_source="aperiodic",
        jitter_cv=0.0,
        gap_count=0,
        max_gap_s=0.0,
        total_silent_s=round(silent, 4),
        stall_silent_s=round(stall_silent, 4),
        estimated_dropped=0,
        dropped_confidence=0.0,
        score=100.0,
    )


def _overlap_seconds(gaps: list[Gap], windows: list[tuple[float, float]]) -> float:
    """How much of this topic's silence fell inside a system-wide stall."""
    if not windows or not gaps:
        return 0.0
    total = 0.0
    for gap in gaps:
        for lo, hi in windows:
            overlap = min(gap.t_end, hi) - max(gap.t_start, lo)
            if overlap > 0:
                total += overlap
    return total


#: a gap this far inside a stall window is the stall, not a separate event
_ROLLUP_CONTAINMENT = 0.8


def _roll_up_stall_gaps(
    findings: list[Finding], stall_windows: list[tuple[float, float]]
) -> list[Finding]:
    """Collapse per-topic gaps that a system-wide stall already explains.

    When the recorder stops, every topic goes silent at once. Reporting that as one
    finding per topic turned an ordinary flight into 900–3000 findings and buried the
    single fact that mattered — the recorder stalled — under its own consequences.

    The gaps are not discarded: the count and the affected topics move onto the stall
    finding as evidence, which is where an engineer would look for them anyway. A gap
    only partly inside a stall window survives on its own, because the part outside is
    genuinely unexplained.
    """
    if not stall_windows:
        return findings

    absorbed: dict[tuple[float, float], list[str]] = {w: [] for w in stall_windows}
    kept: list[Finding] = []
    for f in findings:
        if f.detector != "gap" or f.topic is None:
            kept.append(f)
            continue
        span = f.t_end - f.t_start
        target = None
        if span > 0:
            for lo, hi in stall_windows:
                inside = min(f.t_end, hi) - max(f.t_start, lo)
                if inside / span >= _ROLLUP_CONTAINMENT:
                    target = (lo, hi)
                    break
        if target is None:
            kept.append(f)
        else:
            absorbed[target].append(f.topic)

    for f in kept:
        if f.detector != "correlation" or f.topic is not None:
            continue
        topics = absorbed.get((f.t_start, f.t_end))
        if not topics:
            continue
        f.evidence["gaps_rolled_up"] = float(len(topics))
        f.interpretation += (
            f". The {len(topics)} per-topic silences inside this window are this one "
            f"event seen once per topic, and are reported here rather than separately"
        )
    return kept


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
