"""`logs.*` — text and diagnostics, compressed into patterns rather than lines."""

from __future__ import annotations

import re
from typing import Any, Literal

import numpy as np
from pydantic import BaseModel, Field

from ..budget import apply_budget, make_continuation
from ..kernels.logs import cluster_templates, read_diagnostics, read_log_messages
from ..models import Budgeted, Severity
from ..provenance import Provenance
from .common import audit, resolve


class LogLine(BaseModel):
    t: float
    level: str
    node: str
    message: str


class LogQuery(Budgeted):
    lines: list[LogLine] = Field(default_factory=list)
    total_matched: int = 0
    offset: int = 0
    levels: dict[str, int] = Field(default_factory=dict)
    provenance: Provenance = Field(default_factory=Provenance)


class PatternRow(BaseModel):
    template: str
    level: str
    count: int
    example: str
    first_t: float
    last_t: float
    burst_t: float | None = None


class PatternSet(Budgeted):
    patterns: list[PatternRow] = Field(default_factory=list)
    total_lines: int = 0
    total_patterns: int = 0
    compression: str = ""
    provenance: Provenance = Field(default_factory=Provenance)


class TimelineEvent(BaseModel):
    t: float
    kind: str
    severity: int
    source: str
    summary: str


class EventTimeline(Budgeted):
    events: list[TimelineEvent] = Field(default_factory=list)
    total: int = 0
    provenance: Provenance = Field(default_factory=Provenance)


class LogSignalCorrelation(BaseModel):
    topic: str
    field_path: str
    level: str
    bursts: int
    coincident_excursions: int
    coincidence_rate: float
    verdict: str
    provenance: Provenance = Field(default_factory=Provenance)


def register(mcp: Any) -> None:
    @mcp.tool(name="logs.query")
    def query(
        path: str,
        level: Literal["DEBUG", "INFO", "WARN", "ERROR", "FATAL"] | None = None,
        node: str | None = None,
        pattern: str | None = None,
        start_s: float | None = None,
        end_s: float | None = None,
        limit: int = 100,
        continuation_token: str | None = None,
    ) -> LogQuery:
        """Filtered `/rosout` lines by level, node, regex and time window.

        Prefer logs.cluster_patterns first: forty thousand lines become thirty patterns,
        and only then is it worth reading individual lines from the interesting one.

        Pass a previous result's `continuation_token` to read further down the match list.
        """
        offset = 0
        if continuation_token:
            from ..budget import read_continuation

            offset = int(read_continuation(continuation_token).get("offset", 0))
        p = resolve(path)
        entries = read_log_messages(p)
        rx = re.compile(pattern, re.I) if pattern else None
        matched = [
            e
            for e in entries
            if (level is None or e.level == level)
            and (node is None or node in e.name)
            and (rx is None or rx.search(e.msg))
            and (start_s is None or e.t >= start_s)
            and (end_s is None or e.t <= end_s)
        ]
        levels: dict[str, int] = {}
        for e in matched:
            levels[e.level] = levels.get(e.level, 0) + 1
        page = matched[offset : offset + limit]
        result = LogQuery(
            lines=[LogLine(t=round(e.t, 3), level=e.level, node=e.name, message=e.msg)
                   for e in page],
            total_matched=len(matched),
            offset=offset,
            levels=levels,
            provenance=Provenance(
                path=str(p), method="rosout_filter", sample_count=len(entries),
                time_range=(start_s or 0.0, end_s or (entries[-1].t if entries else 0.0)),
            ),
        )

        def fewer(r: LogQuery) -> LogQuery:
            r.lines = r.lines[: max(10, len(r.lines) // 3)]
            return r

        budgeted = apply_budget(
            result, ladder=(fewer, fewer, fewer),
            narrowing=f"{len(matched)} lines matched — add a level, node or regex filter, "
                      f"or call logs.cluster_patterns instead",
        )
        # `limit` is its own reason to continue, quite apart from the token budget:
        # a page that fits comfortably can still have thousands of lines behind it
        shown = offset + len(budgeted.lines)
        if shown < len(matched):
            budgeted.continuation_token = make_continuation({"offset": shown})
            if not budgeted.suggested_narrowing:
                budgeted.suggested_narrowing = (
                    f"showing lines {offset + 1}–{shown} of {len(matched)}; pass "
                    f"continuation_token for the next page, or filter to narrow instead"
                )
        return budgeted

    @mcp.tool(name="logs.cluster_patterns")
    def cluster_patterns(path: str, min_count: int = 1, limit: int = 40) -> PatternSet:
        """Collapse log lines into templates with counts. The biggest context saving here.

        Numbers, paths, quoted strings and UUIDs are replaced by placeholders, so
        "Timeout on /scan after 1.20s" and "…after 3.40s" become one pattern with a count.
        `burst_t` marks where a pattern clustered in time, which is usually the incident.
        """
        p = resolve(path)
        entries = read_log_messages(p)
        clusters = [c for c in cluster_templates(entries) if c.count >= min_count]

        rows = []
        for c in clusters[:limit]:
            burst = None
            if len(c.times) > 4:
                times = np.asarray(c.times)
                hist, edges = np.histogram(times, bins=min(20, max(4, len(times) // 3)))
                if hist.max() > 3 * max(hist.mean(), 1e-9):
                    burst = float((edges[int(hist.argmax())] + edges[int(hist.argmax()) + 1]) / 2)
            rows.append(
                PatternRow(
                    template=c.template, level=c.level, count=c.count, example=c.example[:200],
                    first_t=round(c.first_t, 3), last_t=round(c.last_t, 3),
                    burst_t=round(burst, 3) if burst else None,
                )
            )
        result = PatternSet(
            patterns=rows,
            total_lines=len(entries),
            total_patterns=len(clusters),
            compression=f"{len(entries)} lines → {len(clusters)} patterns",
            provenance=Provenance(path=str(p), method="drain_style_templating",
                                  sample_count=len(entries)),
        )

        def fewer(r: PatternSet) -> PatternSet:
            r.patterns = r.patterns[: max(5, len(r.patterns) // 2)]
            return r

        return apply_budget(result, ladder=(fewer, fewer), narrowing="raise min_count")

    @mcp.tool(name="logs.timeline")
    def timeline(path: str, min_severity: int = 2, limit: int = 60) -> EventTimeline:
        """One ordered narrative: detector findings, error bursts and diagnostics merged.

        This is the "what happened, in order" view — start here when you know something
        went wrong but not when.
        """
        p = resolve(path)
        report, _auditor = audit(str(p))
        events = [
            TimelineEvent(
                t=round(f.t_start, 3), kind=f.detector, severity=int(f.severity),
                source=f.topic or "recording", summary=f.summary,
            )
            for f in report.findings
            if int(f.severity) >= min_severity
        ]
        for c in cluster_templates(read_log_messages(p)):
            if c.level in ("ERROR", "FATAL") or (c.level == "WARN" and c.count > 5):
                events.append(
                    TimelineEvent(
                        t=round(c.first_t, 3),
                        kind="log_" + c.level.lower(),
                        severity=int(Severity.HIGH if c.level != "WARN" else Severity.MEDIUM),
                        source="rosout",
                        summary=f"{c.count}x {c.template[:120]}",
                    )
                )
        for row in read_diagnostics(p):
            if row["level"] >= 2:
                events.append(
                    TimelineEvent(
                        t=round(row["t"], 3), kind="diagnostic", severity=int(Severity.HIGH),
                        source=row["name"], summary=row["message"],
                    )
                )
        events.sort(key=lambda e: e.t)
        result = EventTimeline(
            events=events[:limit], total=len(events), provenance=report.provenance
        )

        def fewer(r: EventTimeline) -> EventTimeline:
            r.events = [e for e in r.events if e.severity >= 3] or r.events[:10]
            return r

        return apply_budget(result, ladder=(fewer,), narrowing="raise min_severity")

    @mcp.tool(name="logs.correlate_with_signal")
    def correlate_with_signal(
        path: str,
        topic: str,
        field_path: str,
        level: Literal["WARN", "ERROR", "FATAL"] = "ERROR",
        window_s: float = 2.0,
    ) -> LogSignalCorrelation:
        """Do log bursts coincide with a numeric excursion?

        Answers "were those errors caused by the voltage sag, or unrelated?" by checking
        how often an error burst sits within `window_s` of an outlier in the signal.
        """
        from ..kernels.timeseries import rolling_mad_outliers
        from ..readers import open_bag

        p = resolve(path)
        entries = [e for e in read_log_messages(p) if e.level == level]
        reader = open_bag(p)
        t0 = reader.metadata().start_time_ns
        ts, vs = [], []
        for t_ns, value in reader.numeric_field(topic, field_path):
            ts.append((t_ns - t0) / 1e9)
            vs.append(value)
        reader.close()

        outliers = rolling_mad_outliers(np.asarray(ts), np.asarray(vs), k=4.0)
        outlier_times = np.asarray([o[0] for o in outliers])
        coincident = 0
        for e in entries:
            if outlier_times.size and np.min(np.abs(outlier_times - e.t)) <= window_s:
                coincident += 1
        rate = coincident / len(entries) if entries else 0.0
        verdict = (
            "no log lines at that level to correlate" if not entries
            else f"{rate * 100:.0f}% of {level} lines fall within {window_s}s of an excursion in "
                 f"{topic}.{field_path} — "
                 + ("a strong association" if rate > 0.6 else
                    "a partial association" if rate > 0.25 else "no meaningful association")
        )
        return LogSignalCorrelation(
            topic=topic, field_path=field_path, level=level, bursts=len(entries),
            coincident_excursions=coincident, coincidence_rate=round(rate, 3), verdict=verdict,
            provenance=Provenance(
                path=str(p), topics=[topic], method=f"log/signal coincidence(±{window_s}s)",
                sample_count=len(entries) + len(outliers),
            ),
        )
