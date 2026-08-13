"""`export.*` — shareable evidence. Read-only on sources; only ever writes new files."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
from pydantic import BaseModel, Field

from ..artifacts import artifact_path, foxglove_layout, markdown_report, plot_html, trim_mcap
from ..kernels.timeseries import resample
from ..provenance import Provenance
from ..readers import open_bag
from .common import audit, resolve


class ExportResult(BaseModel):
    file: str
    kind: str
    bytes: int = 0
    detail: dict[str, Any] = Field(default_factory=dict)
    note: str = ""
    provenance: Provenance = Field(default_factory=Provenance)


class ReportResult(BaseModel):
    file: str
    markdown: str
    findings: int = 0
    provenance: Provenance = Field(default_factory=Provenance)


def register(mcp: Any) -> None:
    @mcp.tool(name="export.trim_bag")
    def trim_bag(
        path: str,
        start_s: float,
        end_s: float,
        topics: list[str] | None = None,
        out_name: str | None = None,
    ) -> ExportResult:
        """Write a small MCAP containing only the interesting window and topics.

        This is how you hand the evidence to a colleague: a 40 MB slice instead of a
        40 GB recording, containing exactly the failure. The source file is never modified.
        """
        src = resolve(path)
        dst = artifact_path(out_name or f"{src.stem}_{start_s:.0f}-{end_s:.0f}.mcap")
        detail = trim_mcap(src, dst, topics, start_s, end_s)
        return ExportResult(
            file=str(dst), kind="mcap", bytes=detail["bytes"], detail=detail,
            note=f"{detail['messages']} messages between t={start_s:.1f}s and t={end_s:.1f}s",
            provenance=Provenance(path=str(src), topics=topics or [],
                                  time_range=(start_s, end_s), method="trim_mcap",
                                  sample_count=detail["messages"]),
        )

    @mcp.tool(name="export.plot")
    def plot(
        path: str,
        signals: list[str],
        bin_s: float = 0.5,
        title: str | None = None,
        out_name: str | None = None,
    ) -> ExportResult:
        """A standalone interactive HTML plot for a set of signals.

        `signals` are "topic:field.path" strings, e.g. "/odom:twist.twist.linear.x".
        Opens in any browser with no server.
        """
        src = resolve(path)
        reader = open_bag(src)
        t0 = reader.metadata().start_time_ns
        series: dict[str, tuple[list[float], list[float]]] = {}
        for spec in signals[:8]:
            topic, _, field_path = spec.partition(":")
            ts, vs = [], []
            for t_ns, value in reader.numeric_field(topic, field_path):
                ts.append((t_ns - t0) / 1e9)
                vs.append(value)
            if not ts:
                continue
            centres, binned, _gaps = resample(np.asarray(ts), np.asarray(vs), bin_s)
            series[spec] = (
                [round(float(x), 3) for x in centres],
                [None if not np.isfinite(v) else round(float(v), 6) for v in binned],  # type: ignore[list-item]
            )
        reader.close()
        dst = artifact_path(out_name or f"{src.stem}_plot.html")
        plot_html(series, title or f"{src.name} — {', '.join(signals[:3])}", dst)
        return ExportResult(
            file=str(dst), kind="html", bytes=dst.stat().st_size,
            detail={"series": len(series)},
            note="open in a browser; gaps are left blank rather than interpolated",
            provenance=Provenance(path=str(src), method=f"plotly(bin_s={bin_s})",
                                  sample_count=sum(len(v[0]) for v in series.values())),
        )

    @mcp.tool(name="export.foxglove_layout")
    def export_foxglove_layout(
        path: str, topics: list[str], focus_s: float = 0.0, out_name: str | None = None
    ) -> ExportResult:
        """A Foxglove layout JSON pre-pointed at the incident.

        baglens is not a visualiser — Foxglove is, and it is better. Hand off to the tool
        people already use, with the panels and the timestamp already set up.
        """
        src = resolve(path)
        dst = artifact_path(out_name or f"{src.stem}_layout.json")
        foxglove_layout(topics, focus_s, dst)
        return ExportResult(
            file=str(dst), kind="foxglove_layout", bytes=dst.stat().st_size,
            detail={"panels": min(len(topics), 6), "focus_s": focus_s},
            note="import via Foxglove → Layouts → Import from file, then open the recording",
            provenance=Provenance(path=str(src), topics=topics, method="foxglove_layout"),
        )

    @mcp.tool(name="export.report")
    def export_report(path: str, out_name: str | None = None) -> ReportResult:
        """A Markdown investigation report where every claim carries its citation.

        Includes the verdict, the caveats, every finding with the rule that fired and the
        numbers behind it, and the per-topic health table. This is the artifact to paste
        into an issue.
        """
        src = resolve(path)
        report, _auditor = audit(str(src))
        text = markdown_report(report)
        dst = artifact_path(out_name or f"{src.stem}_report.md")
        dst.write_text(text)
        return ReportResult(
            file=str(dst), markdown=text[:4000], findings=len(report.findings),
            provenance=report.provenance,
        )

    _ = Path
