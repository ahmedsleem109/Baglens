"""Shared Pydantic models. Schemas are what the LLM actually reads — treat them as UX."""

from __future__ import annotations

from enum import IntEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

from .provenance import Provenance


class Severity(IntEnum):
    INFO = 0
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4


class Budgeted(BaseModel):
    """Mixin fields attached by the budgeter when a response had to be reduced."""

    truncated: bool = False
    original_size: int | None = None
    continuation_token: str | None = None
    suggested_narrowing: str | None = None


class Finding(BaseModel):
    """One thing that is wrong with a recording."""

    id: str = ""  # stable within a report; the handle for health.explain_finding
    detector: str  # "rate_degradation"
    severity: Severity
    topic: str | None = None
    t_start: float  # seconds from bag start
    t_end: float
    summary: str  # one line, human-readable
    evidence: dict[str, float] = Field(default_factory=dict)
    confidence: float = 1.0
    interpretation: str = ""  # what this usually means
    rule: str = ""  # the exact rule that fired
    provenance: Provenance = Field(default_factory=Provenance)


class TopicHealth(BaseModel):
    topic: str
    msg_type: str = ""
    count: int = 0
    expected_hz: float | None = None
    observed_hz: float = 0.0
    hz_source: Literal["qos", "modal", "declared", "unknown", "aperiodic"] = "modal"
    jitter_cv: float = 0.0
    gap_count: int = 0
    max_gap_s: float = 0.0
    total_silent_s: float = 0.0
    #: of `total_silent_s`, the part that fell inside a system-wide stall. A topic
    #: silenced because the whole recorder stopped did not fail, and is not scored or
    #: billed for dropped messages as though it had.
    stall_silent_s: float = 0.0
    estimated_dropped: int = 0
    dropped_confidence: float = 0.0
    score: float = 100.0


class ChunkIssue(BaseModel):
    kind: Literal["crc_mismatch", "truncated", "unreadable"]
    offset: int = 0
    t_start: float | None = None
    t_end: float | None = None
    detail: str = ""


class FileIntegrity(BaseModel):
    path: str
    format: Literal["mcap", "db3", "bag1", "ulog", "unknown"] = "unknown"
    size_bytes: int = 0
    readable: bool = True
    partial: bool = False  # summary section missing / recovered by scan
    in_progress: bool = False  # observed to grow while being validated
    #: written to very recently — suggestive of an active recording, but not evidence
    recently_modified: bool = False
    has_summary: bool = True
    truncated_bytes: int = 0
    #: what the index promises vs what actually decodes; the gap is corrupt data
    messages_claimed: int = 0
    messages_readable: int = 0
    chunk_issues: list[ChunkIssue] = Field(default_factory=list)

    @property
    def unreadable_fraction(self) -> float:
        if not self.messages_claimed:
            return 0.0
        return max(0.0, 1.0 - self.messages_readable / self.messages_claimed)
    last_readable_time: float | None = None
    score: float = 100.0
    notes: list[str] = Field(default_factory=list)


class Assessability(BaseModel):
    """Whether the recording supports a verdict at all, and why not when it does not.

    Separate from the score on purpose. A score of 0.0 claims the recording is bad; this
    says nothing about the recording's health, only about whether health was measurable.
    Conflating the two is what published a parked shuttle bus as `compromised`.
    """

    assessable: bool = True
    #: the weakest of the four ratios against their floors, clipped to 1.0
    confidence: float = 1.0
    topics_total: int = 0
    topics_assessable: int = 0
    #: share of messages carried by topics that have a measurable rate
    message_fraction: float = 1.0
    #: share of the recording during which any assessable topic published
    coverage_fraction: float = 1.0
    duration_s: float = 0.0
    reasons: list[str] = Field(default_factory=list)


class ClockStep(BaseModel):
    t: float
    delta_s: float
    direction: Literal["forward", "backward"]
    topic: str | None = None


class ClockReport(BaseModel):
    monotonic: bool = True
    backward_jumps: int = 0
    backward_jump_times: list[float] = Field(default_factory=list)
    max_backward_jump_s: float = 0.0
    #: log_time - publish_time, downsampled to <= config.clock.lag_curve_points
    lag_curve_t: list[float] = Field(default_factory=list)
    lag_curve_s: list[float] = Field(default_factory=list)
    lag_start_s: float = 0.0
    lag_end_s: float = 0.0
    lag_growth_s: float = 0.0
    lag_max_s: float = 0.0
    steps: list[ClockStep] = Field(default_factory=list)
    provenance: Provenance = Field(default_factory=Provenance)


class GapDetail(BaseModel):
    topic: str
    t_start: float
    t_end: float
    duration_s: float
    expected_period_s: float
    periods_missed: float
    severity: Severity
    estimated_lost: int = 0
    concurrency: float = 0.0  # D7
    co_silent_topics: list[str] = Field(default_factory=list)
    classification: Literal["system_wide_stall", "subsystem_failure", "isolated_topic", "unknown"] = (
        "unknown"
    )


class Timeline(BaseModel):
    """Compact density map — one row per topic, one column per bucket."""

    t_start: float
    t_end: float
    bucket_s: float
    legend: str = "█ full · ▓ partial · ░ sparse · · silent"
    rows: list[str] = Field(default_factory=list)  # "topic  ██████░░···███"
    provenance: Provenance = Field(default_factory=Provenance)


class AgeStage(BaseModel):
    """One stage of an inferred pipeline, and the age of the data it publishes."""

    topic: str
    #: the topic this one's stamps came from, inferred from stamp equality — never
    #: declared, so `link_observations` is how much support the inference has
    upstream: str | None = None
    link_observations: int = 0
    messages: int = 0
    #: fraction of this topic's messages carrying a stamp no upstream topic had published.
    #: 1.0 marks a chain root — either a genuine sensor or a node that restamped.
    origin_fraction: float = 0.0
    #: False when the topic is too sparse for a per-bucket P99 to be a statistic. Its
    #: ages below are still real; its *trend* was refused rather than guessed.
    trend_assessable: bool = False
    #: age of the data behind this topic's messages, measured from capture
    age_p50_ms: float = 0.0
    age_p95_ms: float = 0.0
    age_p99_ms: float = 0.0
    #: the delay this stage itself adds, measured from its upstream's publish
    stage_p50_ms: float | None = None
    stage_p95_ms: float | None = None
    stage_p99_ms: float | None = None


class DataAgeReport(BaseModel):
    """F1 — how old the data behind each topic was, per stage and end to end."""

    stages: list[AgeStage] = Field(default_factory=list)
    #: topics nothing downstream derives from: the ends of the chains
    endpoints: list[str] = Field(default_factory=list)
    #: topic -> why its age could not be measured. A stage that cannot be measured is
    #: named here rather than being given an age computed from its arrival time.
    unmeasurable: dict[str, str] = Field(default_factory=dict)
    #: ages across unsynchronised clocks are meaningless; when True nothing is reported
    clock_suspect: bool = False
    stamp_table_evictions: int = 0


class BaselineTopic(BaseModel):
    """What one topic looked like on a run that was known good."""

    msg_type: str = ""
    hz: float = 0.0
    jitter_cv: float = 0.0
    #: P95 data age in ms, when it was measurable. None means it was not, and the gate
    #: must then report age unchecked for this topic rather than assume it is fine.
    age_p95_ms: float | None = None


class Baseline(BaseModel):
    """A fleet baseline: what "normal" is, captured rather than hand-written.

    Written by `baglens preflight --record` from a run someone was willing to call good.
    Hand-writing this file is possible and is a bad idea — the numbers that matter are
    the ones the robot actually produces, not the ones in the datasheet.
    """

    version: int = 1
    source: str = ""
    captured_duration_s: float = 0.0
    topics: dict[str, BaselineTopic] = Field(default_factory=dict)


class PreflightCheck(BaseModel):
    """One question the gate asked, and what it found.

    `status` has three values, not two. `unchecked` is the one that matters: a topic too
    slow to establish a baseline inside the window has not passed, and saying so is the
    difference between a gate and a rubber stamp.
    """

    check: str  # topic_present | rate | clock | degrading | data_age | tf
    topic: str | None = None
    status: Literal["pass", "fail", "unchecked"] = "pass"
    detail: str = ""
    observed: float | None = None
    expected: float | None = None


class PreflightReport(BaseModel):
    """Is this robot fit to record a mission? Green or red, with reasons."""

    verdict: Literal["go", "no_go"] = "go"
    window_s: float = 0.0
    elapsed_s: float = 0.0
    messages: int = 0
    checks: list[PreflightCheck] = Field(default_factory=list)
    #: one line per reason the answer is no_go — what a human reads first
    failures: list[str] = Field(default_factory=list)
    #: everything the gate could not judge. Never silently empty.
    unchecked: list[str] = Field(default_factory=list)
    baseline_source: str = ""
    provenance: Provenance = Field(default_factory=Provenance)


class HealthReport(Budgeted):
    mission_id: str
    path: str = ""
    duration_s: float = 0.0
    overall_score: float = 100.0
    #: `unassessable` is not a worse grade than `compromised` — it is a refusal to grade.
    #: When it is set, `overall_score` is the score of the part that could be measured and
    #: must not be read as a judgement of the recording; `assessability.reasons` says why.
    verdict: Literal["trustworthy", "usable_with_caveats", "compromised", "unassessable"] = (
        "trustworthy"
    )
    assessability: Assessability | None = None
    findings: list[Finding] = Field(default_factory=list)
    topics: list[TopicHealth] = Field(default_factory=list)
    file_integrity: FileIntegrity | None = None
    clock: ClockReport | None = None
    data_age: DataAgeReport | None = None
    #: what an analyst must NOT conclude from this data
    caveats: list[str] = Field(default_factory=list)
    provenance: Provenance = Field(default_factory=Provenance)


class FindingDetail(BaseModel):
    finding: Finding
    rule: str
    evidence: dict[str, Any] = Field(default_factory=dict)
    next_steps: list[str] = Field(default_factory=list)
    related_findings: list[str] = Field(default_factory=list)
