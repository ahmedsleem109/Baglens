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
    hz_source: Literal["qos", "modal", "declared", "unknown"] = "modal"
    jitter_cv: float = 0.0
    gap_count: int = 0
    max_gap_s: float = 0.0
    total_silent_s: float = 0.0
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
    in_progress: bool = False  # file still growing
    has_summary: bool = True
    truncated_bytes: int = 0
    chunk_issues: list[ChunkIssue] = Field(default_factory=list)
    last_readable_time: float | None = None
    score: float = 100.0
    notes: list[str] = Field(default_factory=list)


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


class HealthReport(Budgeted):
    mission_id: str
    path: str = ""
    duration_s: float = 0.0
    overall_score: float = 100.0
    verdict: Literal["trustworthy", "usable_with_caveats", "compromised"] = "trustworthy"
    findings: list[Finding] = Field(default_factory=list)
    topics: list[TopicHealth] = Field(default_factory=list)
    file_integrity: FileIntegrity | None = None
    clock: ClockReport | None = None
    #: what an analyst must NOT conclude from this data
    caveats: list[str] = Field(default_factory=list)
    provenance: Provenance = Field(default_factory=Provenance)


class FindingDetail(BaseModel):
    finding: Finding
    rule: str
    evidence: dict[str, Any] = Field(default_factory=dict)
    next_steps: list[str] = Field(default_factory=list)
    related_findings: list[str] = Field(default_factory=list)
