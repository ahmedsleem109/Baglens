"""`health.*` — the integrity auditor. The headline namespace.

Before you debug the robot, verify the recording.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from ..budget import apply_budget, estimate_tokens, make_continuation
from ..config import CONFIG
from ..models import (
    ClockReport,
    FileIntegrity,
    Finding,
    FindingDetail,
    GapDetail,
    HealthReport,
    Severity,
    Timeline,
)
from ..provenance import Provenance
from .common import audit, find_finding, resolve


class TopicQos(BaseModel):
    topic: str
    msg_type: str = ""
    observed_hz: float = 0.0
    declared_hz: float | None = None
    reliability: str = "unknown"
    durability: str = "unknown"
    history: str = "unknown"
    depth: int = 0
    deadline_s: float | None = None
    recorded: bool = True


class QosIssueModel(BaseModel):
    topic: str
    kind: str
    severity: str
    detail: str
    recommendation: str


class QosReport(BaseModel):
    topics: list[TopicQos] = Field(default_factory=list)
    issues: list[QosIssueModel] = Field(default_factory=list)
    topics_without_qos: list[str] = Field(default_factory=list)
    verdict: str = ""
    provenance: Provenance = Field(default_factory=Provenance)


class GapList(BaseModel):
    gaps: list[GapDetail] = Field(default_factory=list)
    total_gaps: int = 0
    offset: int = 0
    truncated: bool = False
    continuation_token: str | None = None
    suggested_narrowing: str | None = None
    provenance: Provenance = Field(default_factory=Provenance)


def _trim_findings(report: HealthReport) -> HealthReport:
    """Ladder step 1: keep the worst findings, summarise the rest."""
    keep = CONFIG.budget.max_findings
    if len(report.findings) <= keep:
        return report
    dropped = len(report.findings) - keep
    report.findings = report.findings[:keep]
    report.caveats.append(
        f"{dropped} lower-severity findings were omitted to fit the response budget — "
        f"call health.find_gaps or re-run with a topic filter to see them."
    )
    return report


def _trim_topics(report: HealthReport) -> HealthReport:
    """Ladder step 2: healthy topics collapse to a count; broken ones stay."""
    unhealthy = [t for t in report.topics if t.score < 99.0 or t.gap_count]
    if len(unhealthy) < len(report.topics):
        report.caveats.append(
            f"{len(report.topics) - len(unhealthy)} topics scored clean and were omitted "
            f"from the per-topic list."
        )
        report.topics = unhealthy
    return report


def _trim_evidence(report: HealthReport) -> HealthReport:
    """Ladder step 3: drop the numbers, keep the claims. explain_finding restores them."""
    for f in report.findings:
        f.evidence = {}
        f.interpretation = f.interpretation[:120]
    if report.clock is not None:
        report.clock.lag_curve_t = report.clock.lag_curve_t[:20]
        report.clock.lag_curve_s = report.clock.lag_curve_s[:20]
    return report


def register(mcp: Any) -> None:
    @mcp.tool(name="health.audit_recording")
    def audit_recording(
        path: str,
        topics: list[str] | None = None,
        detectors: list[str] | None = None,
        sensitivity: Literal["low", "normal", "high"] = "normal",
    ) -> HealthReport:
        """Run a full integrity audit on a robot recording.

        ALWAYS call this before drawing conclusions from a bag — it tells you whether
        the data can support the analysis you are about to do. Returns findings ranked
        by severity, a per-topic health table, and explicit `caveats` describing what
        this recording cannot prove.

        Start here, then use health.explain_finding to drill into a specific finding,
        health.find_gaps to see whether a silence was system-wide, and
        health.clock_report for the recorder-lag curve.

        `sensitivity` scales the gap and trend thresholds; use "high" on a quiet fleet
        and "low" on a noisy one. Safe on damaged and in-progress files.
        """
        report, _ = audit(path, topics, detectors, sensitivity)
        return apply_budget(
            report.model_copy(deep=True),
            ladder=(_trim_findings, _trim_topics, _trim_evidence),
            narrowing=(
                "pass topics=[...] to audit a subset, or detectors=['gap','clock'] to "
                "run fewer checks"
            ),
        )

    @mcp.tool(name="health.find_gaps")
    def find_gaps(
        path: str,
        topic: str | None = None,
        min_duration_s: float = 0.0,
        sensitivity: Literal["low", "normal", "high"] = "normal",
        continuation_token: str | None = None,
    ) -> GapList:
        """List every silence, with the co-silent topics that explain it.

        This is the tool that answers "did the sensor die, or did the recorder stall?".
        Each gap carries a `concurrency` score and a `classification`:
        system_wide_stall (recorder, disk, CPU or power), subsystem_failure (a shared
        driver or bus — read `co_silent_topics`, that list is the diagnosis), or
        isolated_topic (that sensor or node alone).

        Gaps come back longest first. If the result is truncated, pass its
        `continuation_token` back to walk further down the list.
        """
        offset = 0
        if continuation_token:
            from ..budget import read_continuation

            offset = int(read_continuation(continuation_token).get("offset", 0))
        report, auditor = audit(path, [topic] if topic else None, None, sensitivity)
        gaps = auditor.all_gaps()
        details = (
            auditor.correlation.classify(gaps)
            if auditor.correlation is not None
            else []
        )
        details = [d for d in details if d.duration_s >= min_duration_s]
        if topic:
            details = [d for d in details if d.topic == topic]
        details.sort(key=lambda d: -d.duration_s)

        total = len(details)
        page = details[offset:]
        out = GapList(gaps=page, total_gaps=total, offset=offset, provenance=report.provenance)
        limit = CONFIG.budget.max_tokens
        while estimate_tokens(out) > limit and len(out.gaps) > 5:
            out.gaps = out.gaps[: max(5, len(out.gaps) // 2)]
            out.truncated = True
        if out.truncated:
            shown = offset + len(out.gaps)
            out.continuation_token = make_continuation({"offset": shown})
            out.suggested_narrowing = (
                f"showing gaps {offset + 1}–{shown} of {total}, longest first. Pass "
                f"min_duration_s or topic= to narrow, or continuation_token to page on."
            )
        return out

    @mcp.tool(name="health.qos_report")
    def qos_report(path: str) -> QosReport:
        """The recorded QoS profile per topic, and the profiles that cause silent drops.

        QoS is where data loss is *configured*: BEST_EFFORT permits the middleware to
        drop under load, a shallow KEEP_LAST queue discards as soon as a subscriber
        stalls, and a declared deadline nobody honours makes every downstream timeout
        wrong. None of that shows up in a message count.

        Call this when messages are missing and the gaps look diffuse — it distinguishes
        "the sensor failed" from "this topic was configured to be lossy".
        """
        from ..kernels.qos import check_profile, parse_qos

        report, auditor = audit(path)
        meta = auditor.meta
        rows: list[TopicQos] = []
        issues: list[QosIssueModel] = []
        missing: list[str] = []
        health_by_topic = {t.topic: t for t in report.topics}

        for info in meta.topics:
            health = health_by_topic.get(info.topic)
            observed = health.observed_hz if health else 0.0
            drop_rate = 0.0
            if health and health.count:
                drop_rate = health.estimated_dropped / max(
                    health.count + health.estimated_dropped, 1
                )
            profile = parse_qos(info.qos)
            if profile is None:
                missing.append(info.topic)
                rows.append(
                    TopicQos(topic=info.topic, msg_type=info.msg_type,
                             observed_hz=round(observed, 3), recorded=False)
                )
                continue
            rows.append(
                TopicQos(
                    topic=info.topic,
                    msg_type=info.msg_type,
                    observed_hz=round(observed, 3),
                    declared_hz=round(profile.declared_hz, 3) if profile.declared_hz else None,
                    reliability=profile.reliability,
                    durability=profile.durability,
                    history=profile.history,
                    depth=profile.depth,
                    deadline_s=profile.deadline_s,
                )
            )
            issues += [
                QosIssueModel(**issue.__dict__)
                for issue in check_profile(info.topic, profile, observed, drop_rate)
            ]

        issues.sort(key=lambda i: {"high": 0, "medium": 1, "low": 2}.get(i.severity, 3))
        if missing and len(missing) == len(meta.topics):
            verdict = (
                "this recording carries no QoS profiles at all — either the format does "
                "not store them or the recorder did not write them, so nothing here can "
                "be checked"
            )
        elif not issues:
            verdict = "no QoS profile in this recording is likely to cause silent loss"
        else:
            verdict = (
                f"{len(issues)} QoS finding(s); the highest concern is "
                f"{issues[0].kind} on {issues[0].topic}"
            )

        return QosReport(
            topics=rows,
            issues=issues,
            topics_without_qos=missing,
            verdict=verdict,
            provenance=Provenance(
                path=str(resolve(path)),
                mission_id=report.mission_id,
                topics=[t.topic for t in meta.topics],
                time_range=(0.0, report.duration_s),
                method="qos_profile_check",
                sample_count=len(meta.topics),
            ),
        )

    @mcp.tool(name="health.clock_report")
    def clock_report(path: str) -> ClockReport:
        """Clock sanity in full: monotonicity, clock steps, and the recorder-lag curve.

        The lag curve (`log_time - publish_time` over the run) is the one to look at
        when messages are missing but the disk was not saturated: a rising curve means
        the recorder was falling behind the publishers, and diffuse message loss
        follows. Downsampled to 100 points.
        """
        _, auditor = audit(path, None, ["clock"], CONFIG.sensitivity)
        if auditor.clock is None:
            return ClockReport()
        return auditor.clock.report(Provenance(path=str(resolve(path)), method="clock_detector"))

    @mcp.tool(name="health.topic_timeline")
    def topic_timeline(path: str, width: int = 100) -> Timeline:
        """A text density map: one row per topic, one column per time bucket.

        One glance shows the shape of the failure — whether a silence hit one topic or
        all of them, and whether a topic faded rather than stopped. Cheap: ~300 tokens
        for a whole mission.
        """
        report, auditor = audit(path)
        return auditor.timeline.render(report.provenance, width=width)

    @mcp.tool(name="health.validate_file")
    def validate_file_tool(path: str) -> FileIntegrity:
        """Structural integrity check. Safe on damaged, truncated and in-progress files.

        Never raises: if the file is broken it reports how far it could read, how many
        bytes were lost, and which time range is untrustworthy. Use this first when a
        recording will not open in other tools.
        """
        from ..readers.recovery import validate_file

        return validate_file(resolve(path))

    @mcp.tool(name="health.explain_finding")
    def explain_finding(finding_id: str, path: str | None = None) -> FindingDetail:
        """Given a finding id from a previous audit, return the full evidence.

        Includes the exact rule that fired, every number behind the claim, and the
        suggested next steps. Use this instead of re-running the audit at a higher
        sensitivity when you want to know *why* something was flagged.
        """
        if path:
            audit(path)
        hit = find_finding(finding_id)
        if hit is None:
            return FindingDetail(
                finding=Finding(
                    detector="unknown", severity=Severity.INFO, t_start=0, t_end=0,
                    summary=f"no finding {finding_id} in this session's audits",
                ),
                rule="",
                next_steps=["call health.audit_recording on the recording first"],
            )
        finding, report = hit
        return FindingDetail(
            finding=finding,
            rule=finding.rule,
            evidence={
                **finding.evidence,
                "verdict": report.verdict,
                "overall_score": report.overall_score,
                "duration_s": report.duration_s,
            },
            next_steps=_next_steps(finding),
            related_findings=[
                f.id
                for f in report.findings
                if f.id != finding.id
                and f.t_start <= finding.t_end
                and finding.t_start <= f.t_end
            ][:10],
        )


def _next_steps(f: Finding) -> list[str]:
    match f.detector:
        case "gap":
            return [
                "health.find_gaps to see whether other topics were silent at the same time",
                "health.topic_timeline to see the shape of the outage",
                "if concurrency is high, investigate the recorder host, not the sensor",
            ]
        case "rate_degradation":
            return [
                "timeseries.extract on a CPU or temperature topic over the same window",
                "compare.find_similar to check whether this drift appeared in earlier missions",
            ]
        case "jitter":
            return [
                "logs.query for scheduler or timeout warnings in the same window",
                "health.clock_report to rule out a clock problem masquerading as jitter",
            ]
        case "dropped":
            return [
                "health.clock_report — a rising recorder-lag curve explains diffuse drops",
                "health.find_gaps — clustered drops show up there instead",
            ]
        case "clock" | "clock_step" | "clock_lag":
            return [
                "health.clock_report for the full curve and every step",
                "treat time-range queries across the affected instants as unreliable",
            ]
        case "correlation":
            return [
                "check recorder host CPU, disk throughput and power at that timestamp",
                "export.trim_bag around the window to share the evidence",
            ]
        case "file_integrity":
            return [
                "health.validate_file for the byte-level detail",
                "re-copy the file from the robot if the source is still available",
            ]
    return ["health.audit_recording for the full context"]
