"""Artifact writers: trimmed bags, Plotly HTML, Foxglove layouts, Markdown reports.

Not a visualiser. Foxglove exists and is better — emit something it can open and
step aside.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import CONFIG
from .models import Finding, HealthReport


def artifact_path(name: str) -> Path:
    d = CONFIG.artifact_dir
    d.mkdir(parents=True, exist_ok=True)
    return d / name


def trim_mcap(
    src: Path, dst: Path, topics: list[str] | None, start_s: float, end_s: float
) -> dict[str, Any]:
    """Copy a time window into a new MCAP. The only writer in the codebase, and it only
    ever writes a *new* file — nothing can modify a source recording."""
    from mcap.reader import make_reader
    from mcap.writer import CompressionType, Writer

    kept = 0
    with src.open("rb") as fin:
        reader = make_reader(fin)
        summary = reader.get_summary()
        t0 = summary.statistics.message_start_time if summary and summary.statistics else 0
        start_ns = int(t0 + start_s * 1e9)
        end_ns = int(t0 + end_s * 1e9)

        dst.parent.mkdir(parents=True, exist_ok=True)
        with dst.open("wb") as fout:
            writer = Writer(fout, compression=CompressionType.ZSTD)
            writer.start(profile="ros2", library="baglens-export")
            schema_ids: dict[int, int] = {}
            channel_ids: dict[int, int] = {}
            for schema, channel, message in reader.iter_messages(
                topics=topics, start_time=start_ns, end_time=end_ns, log_time_order=True
            ):
                if channel.id not in channel_ids:
                    sid = 0
                    if schema is not None:
                        if schema.id not in schema_ids:
                            schema_ids[schema.id] = writer.register_schema(
                                schema.name, schema.encoding, schema.data
                            )
                        sid = schema_ids[schema.id]
                    channel_ids[channel.id] = writer.register_channel(
                        topic=channel.topic,
                        message_encoding=channel.message_encoding,
                        schema_id=sid,
                        metadata=dict(channel.metadata),
                    )
                writer.add_message(
                    channel_id=channel_ids[channel.id],
                    log_time=message.log_time,
                    publish_time=message.publish_time,
                    data=message.data,
                    sequence=message.sequence,
                )
                kept += 1
            writer.finish()
    return {"messages": kept, "bytes": dst.stat().st_size if dst.exists() else 0}


def plot_html(
    series: dict[str, tuple[list[float], list[float | None]]], title: str, dst: Path
) -> Path:
    import plotly.graph_objects as go

    fig = go.Figure()
    for name, (t, v) in series.items():
        fig.add_trace(go.Scatter(x=t, y=v, mode="lines", name=name))
    fig.update_layout(
        title=title, xaxis_title="t (s from bag start)", yaxis_title="value",
        template="plotly_dark", hovermode="x unified",
    )
    dst.parent.mkdir(parents=True, exist_ok=True)
    fig.write_html(str(dst), include_plotlyjs="cdn")
    return dst


def foxglove_layout(topics: list[str], focus_s: float, dst: Path) -> Path:
    """A layout pre-pointed at the incident: plots for the named topics, playback parked
    at the interesting timestamp."""
    panels: dict[str, Any] = {}
    #: Foxglove accepts either a single panel id or a split node here
    layout: Any = {}
    for i, topic in enumerate(topics[:6]):
        pid = f"Plot!{i}"
        panels[pid] = {
            "paths": [{"value": f"{topic}", "enabled": True, "timestampMethod": "receiveTime"}],
            "showXAxisLabels": True,
            "showYAxisLabels": True,
            "title": topic,
        }
    ids = list(panels)
    if len(ids) == 1:
        layout = ids[0]
    elif ids:
        layout = {"first": ids[0], "second": ids[1] if len(ids) > 1 else ids[0],
                  "direction": "row", "splitPercentage": 50}
    doc = {
        "configById": panels,
        "globalVariables": {"focus_time_s": focus_s},
        "userNodes": {},
        "playbackConfig": {"speed": 1},
        "layout": layout,
    }
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(doc, indent=2))
    return dst


def markdown_report(report: HealthReport, extra_findings: list[Finding] | None = None) -> str:
    """An investigation report where every claim carries its citation."""
    lines = [
        f"# Recording audit — {Path(report.path).name}",
        "",
        f"**Verdict: {report.verdict}** (score {report.overall_score:.1f}/100, "
        f"{report.duration_s:.1f}s, {len(report.topics)} topics)",
        "",
    ]
    if report.caveats:
        lines += ["## What this recording cannot tell you", ""]
        lines += [f"- {c}" for c in report.caveats]
        lines.append("")

    findings = list(report.findings) + list(extra_findings or [])
    if findings:
        lines += ["## Findings", ""]
        for f in findings:
            cite = f.provenance.cite() if f.provenance.path else f.rule
            lines += [
                f"### {f.severity.name} — {f.summary}",
                "",
                f"{f.interpretation}",
                "",
                f"- rule: `{f.rule}`",
                f"- evidence: {', '.join(f'{k}={v}' for k, v in list(f.evidence.items())[:8])}",
                f"- source: `{cite}`",
                "",
            ]

    lines += ["## Topics", "", "| topic | count | expected Hz | observed Hz | jitter CV | gaps | dropped | score |",
              "|---|---|---|---|---|---|---|---|"]
    for t in report.topics:
        expected = f"{t.expected_hz:.2f}" if t.expected_hz else "—"
        lines.append(
            f"| `{t.topic}` | {t.count} | {expected} | {t.observed_hz:.2f} | "
            f"{t.jitter_cv:.3f} | {t.gap_count} | {t.estimated_dropped} | {t.score:.0f} |"
        )
    if report.clock is not None:
        lines += [
            "",
            "## Clock",
            "",
            f"- monotonic: {report.clock.monotonic}",
            f"- recorder lag: {report.clock.lag_start_s * 1000:.0f} ms → "
            f"{report.clock.lag_end_s * 1000:.0f} ms (peak {report.clock.lag_max_s * 1000:.0f} ms)",
            f"- clock steps: {len(report.clock.steps)}",
        ]
    lines += ["", "---", "", f"Generated by baglens from `{report.path}`.",
              f"Mission id `{report.mission_id}`."]
    return "\n".join(lines) + "\n"
