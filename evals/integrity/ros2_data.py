"""What the detectors say about real ROS 2 recordings.

Until this existed, every claim in this repository about ROS 2 rested on synthetic
fixtures the repository itself generated — which can only prove that the detectors and
the generator agree about what a fault looks like. ROS 2 is the target market, so that
was the largest gap in the project and the reason P1.3 blocked the phase.

**This is not a precision/recall measurement, and must not be presented as one.** There
are no labels. Nothing in these recordings says where a fault was, and inventing one by
inspection would be scoring the detectors against their own author. What it measures is
narrower and still worth having:

* the readers open real ROS 2 MCAP written by other people's tooling, not by us;
* the audit terminates, in bounded memory, and produces a report;
* the findings it does produce are *inspectable* — each one is printed with the evidence
  behind it, so a reader can judge whether it is plausible.

Judge the output, do not trust the summary. A finding here is a hypothesis about a
recording nobody has labelled.

    uv run python -m evals.integrity.ros2_data --dir ~/data/public/ros2
"""

from __future__ import annotations

import argparse
import resource
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from baglens.detectors import Auditor
from baglens.readers import open_bag

#: What each recording is, so a reader knows how many *platforms* are represented rather
#: than only how many files. Three files from one robot is one robot, and a claim about
#: "real ROS 2 data" that rests on six runs of the same rig is worth about as much as a
#: claim resting on one.
#:
#: `native` marks recordings written by `ros2 bag record` on the robot itself. Only those
#: can support any claim about *recorder* behaviour — a converted recording was
#: re-timestamped on its way into MCAP, so its arrival stream is the converter's, not the
#: robot's, and a stall in it would be an artefact.
PROVENANCE = {
    "nuway_waypoints": ("autonomous shuttle bus", "xrkong/nuway_rosbag", True),
    "nuway_stops": ("autonomous shuttle bus", "xrkong/nuway_rosbag", True),
    "tesla3_av": ("road vehicle (Tesla Model 3)", "tfoldi/tesla3_av_rosbags", True),
    "uniflex_imu": ("quadruped / IMU rig", "UniflexAI/rosbag2_imu_example", True),
    "fastlivo_hku2": ("handheld LiDAR-inertial-visual rig", "DapengFeng/MCAP", False),
    "demo": ("mixed sample", "MCAP project test corpus", False),
}
for _i in range(6):
    PROVENANCE[f"dongkkka_0{_i}"] = ("short-run rig", "Dongkkka/rosbag_test", True)


@dataclass
class Audited:
    name: str
    platform: str
    source: str
    native: bool
    fmt: str
    size_mb: float
    duration_s: float
    topics: int
    messages: int
    verdict: str
    score: float
    elapsed_s: float
    peak_rss_mb: float
    findings: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: topics D1 could not fit a cadence to, and how many topics there are in total.
    #: A recording that is mostly these is one this tool is known to over-report on.
    unassessable: int = 0
    error: str = ""


def audit(path: Path, min_messages: int = 100) -> Audited | None:
    stem = path.stem
    platform, source, native = PROVENANCE.get(stem, ("unknown", "unknown", False))
    reader = open_bag(path)
    meta = reader.metadata()
    if meta.message_count < min_messages:
        return None

    started = time.perf_counter()
    auditor = Auditor(reader)
    try:
        report = auditor.run()
    except Exception as exc:  # noqa: BLE001 - a reader that cannot cope is the result
        return Audited(stem, platform, source, native, meta.format, meta.size_bytes / 1e6,
                       meta.duration_s, len(meta.topics), meta.message_count, "error", 0.0,
                       time.perf_counter() - started, 0.0,
                       error=f"{type(exc).__name__}: {exc}")
    elapsed = time.perf_counter() - started

    return Audited(
        name=stem,
        platform=platform,
        source=source,
        native=native,
        fmt=meta.format,
        size_mb=meta.size_bytes / 1e6,
        duration_s=meta.duration_s,
        topics=len(meta.topics),
        messages=meta.message_count,
        verdict=report.verdict,
        score=report.overall_score,
        elapsed_s=elapsed,
        peak_rss_mb=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0,
        findings=[
            {
                "detector": f.detector,
                "topic": f.topic or "—",
                "t_start": round(f.t_start, 1),
                "t_end": round(f.t_end, 1),
                "severity": int(f.severity),
                "summary": f.summary,
            }
            for f in sorted(report.findings, key=lambda f: (-int(f.severity), f.t_start))
        ],
        warnings=list(meta.warnings),
        unassessable=len(auditor.unassessable),
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dir", required=True)
    ap.add_argument("--out", default="evals/integrity/ROS2_DATA.md")
    args = ap.parse_args(argv)

    root = Path(args.dir).expanduser()
    audited = []
    for path in sorted(root.iterdir()):
        if path.suffix.lower() not in (".mcap", ".db3", ".bag"):
            continue
        result = audit(path)
        if result is None:
            print(f"{path.name}: too few messages to audit, skipped")
            continue
        audited.append(result)
        print(f"{result.name}: {result.verdict} score={result.score:.1f} "
              f"findings={len(result.findings)} in {result.elapsed_s:.1f}s")

    if not audited:
        print("no recordings audited")
        return 1

    platforms = sorted({a.platform for a in audited})
    native = [a for a in audited if a.native]
    native_platforms = sorted({a.platform for a in native})
    formats = sorted({a.fmt for a in audited})
    total_minutes = sum(a.duration_s for a in audited) / 60

    lines = [
        "# Real ROS 2 recordings",
        "",
        f"**{len(audited)} recordings, {len(platforms)} platforms, {total_minutes:.0f} "
        f"minutes, {sum(a.size_mb for a in audited)/1000:.1f} GB.** Formats exercised: "
        f"{', '.join(f'`{f}`' for f in formats)}. Generated {date.today()}.",
        "",
        f"{len(native)} of them, across {len(native_platforms)} platforms, were written by "
        "`ros2 bag record` on the robot itself. That distinction is load-bearing: a "
        "recording converted into MCAP from something else was re-timestamped on the way, "
        "so its arrival stream belongs to the converter and no claim about *recorder* "
        "behaviour can rest on it. Converted recordings are still worth auditing — they "
        "exercise the readers on real topic sets and real message mixes — but they are "
        "marked, and they are not evidence about recorders.",
        "",
        "**There are no labels here, so there is no precision or recall on this page.** "
        "Nothing in these recordings says where a fault was. What this shows is that the "
        "readers open real ROS 2 files written by other people's tooling, that the audit "
        "completes in bounded memory, and what the detectors claim — each finding printed "
        "with its evidence so it can be judged rather than believed. Measured precision "
        "and recall live in `REAL_DATA.md` (PX4's own dropout records) and in "
        "`INJECTED.md`, which puts exact labels onto copies of *these* recordings by "
        "injecting known faults into them — that is how this corpus acquires ground truth "
        "without anyone here inventing it.",
        "",
        "## By platform",
        "",
        "| Platform | Recordings | Native | Minutes | Topics (max) | Verdicts |",
        "|---|---|---|---|---|---|",
    ]
    for p in platforms:
        group = [a for a in audited if a.platform == p]
        verdicts = ", ".join(
            f"{sum(1 for a in group if a.verdict == v)}× {v}"
            for v in sorted({a.verdict for a in group})
        )
        lines.append(
            f"| {p} | {len(group)} | {sum(1 for a in group if a.native)} | "
            f"{sum(a.duration_s for a in group)/60:.0f} | "
            f"{max(a.topics for a in group)} | {verdicts} |"
        )

    lines += [
        "",
        "## Every recording",
        "",
        "| Recording | Platform | Format | Native | Size | Duration | Topics | Messages | Verdict | Score | Audit | Peak RSS |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for a in audited:
        lines.append(
            f"| `{a.name}` | {a.platform} | `{a.fmt}` | {'yes' if a.native else 'converted'} | "
            f"{a.size_mb:.0f} MB | {a.duration_s:.0f}s | "
            f"{a.topics} | {a.messages:,} | {a.verdict} | {a.score:.1f} | "
            f"{a.elapsed_s:.1f}s | {a.peak_rss_mb:.0f} MB |"
        )

    mostly_event_driven = [a for a in audited if a.topics and a.unassessable > a.topics / 2]
    if mostly_event_driven:
        lines += [
            "",
            "## Known limitation, stated before the results",
            "",
            "Some of these recordings are mostly **event-driven** topics — topics with no "
            "cadence to be late against, which publish when something happens. "
            + ", ".join(f"`{a.name}` ({a.unassessable} of {a.topics})"
                        for a in mostly_event_driven)
            + ". On those, `correlation` over-reports: several topics falling quiet at "
            "once looks like the recorder stopping, and a stationary vehicle does that "
            "constantly. The score and verdict below should not be read as a judgement of "
            "the recording.",
            "",
            "The obvious fix — refusing to let event-driven topics count — was tried four "
            "ways and each cost 22+ points of recall against the PX4 labels, because when "
            "the recorder truly stops those topics stop too. Rather than tune against data "
            "with no labels, the limitation is published. See W15 in `PHASE3.md`.",
        ]

    lines += ["", "## Every finding, so it can be judged", ""]
    for a in audited:
        lines.append(f"### `{a.name}` — {a.platform}")
        lines.append("")
        lines.append(f"Source: {a.source}. "
                     f"{a.unassessable} of {a.topics} topics have no measurable cadence.")
        lines.append("")
        if a.error:
            lines += [f"**Audit failed:** {a.error}", ""]
            continue
        if not a.findings:
            lines += ["No findings.", ""]
        else:
            lines.append("| Detector | Topic | Window | Severity | Claim |")
            lines.append("|---|---|---|---|---|")
            for f in a.findings[:15]:
                lines.append(
                    f"| `{f['detector']}` | `{f['topic']}` | {f['t_start']}–{f['t_end']}s | "
                    f"{f['severity']} | {f['summary']} |"
                )
            if len(a.findings) > 15:
                lines.append(f"| … | | | | {len(a.findings) - 15} more |")
            lines.append("")
        for w in a.warnings:
            lines.append(f"> {w}")
        lines.append("")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n")
    print(f"\nwrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
