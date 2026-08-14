"""Precision and recall on **real** recordings with **real** labels.

Every other number in this repository is scored against faults this repository injected,
which can only prove that the detectors and the generator agree about what a fault looks
like. This eval closes that gap.

PX4's logger writes a dropout record into the ULog whenever it could not keep up. That
record is ground truth authored by the flight controller — not by us, not by a generator,
and written years before this project existed. Scoring the correlation detector against
it is the closest thing to an honest field measurement available without instrumenting a
robot ourselves.

Ground truth : ULog dropout records (start, duration), merged, above a duration floor.
Prediction   : `correlation` findings — the detector that claims "the recorder stalled,
               not a sensor". A logger dropout is exactly that claim, so the two are
               directly comparable.

Corpus hygiene: review.px4.io serves the same flight under more than one UUID, so files
are deduplicated by content hash before scoring. Without that, one popular flight counts
several times and the numbers quietly become a measure of that one flight.

Requires the `ulog` extra:  uv sync --extra ulog

Usage:
    python -m evals.integrity.real_data --dir ~/data/public/px4 --out evals/integrity/REAL_DATA.md
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import traceback
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from baglens.detectors.auditor import Auditor
from baglens.readers.ulog_reader import UlogReader

Interval = tuple[float, float]


@dataclass
class FlightScore:
    name: str
    duration_s: float
    n_truth: int
    n_pred: int
    matched_truth: int
    matched_pred: int
    truth_seconds: float
    blackout_seconds: float


def merge(intervals: list[Interval], slack: float = 0.0) -> list[Interval]:
    """Merge overlapping intervals. Two dropout marks 200 ms apart are one stall."""
    if not intervals:
        return []
    out: list[Interval] = []
    for lo, hi in sorted(intervals):
        if out and lo - out[-1][1] <= slack:
            out[-1] = (out[-1][0], max(out[-1][1], hi))
        else:
            out.append((lo, hi))
    return out


def overlaps(a: Interval, b: Interval, tol: float) -> bool:
    return a[0] - tol <= b[1] and b[0] - tol <= a[1]


def truth_intervals(log: Any, t0: float, min_ms: float) -> list[Interval]:
    """Dropout records the flight controller wrote about itself."""
    raw: list[Interval] = []
    for d in getattr(log, "dropouts", []) or []:
        dur = d.duration / 1000.0  # ULog stores milliseconds
        if dur * 1000.0 < min_ms:
            continue
        start = d.timestamp / 1e6 - t0
        raw.append((start, start + dur))
    return merge(raw, slack=0.5)


def score_flight(path: Path, min_ms: float, tol: float) -> tuple[FlightScore, list[str]]:
    reader = UlogReader(path)
    meta = reader.metadata()
    t0 = meta.start_time_ns / 1e9
    duration = (meta.end_time_ns - meta.start_time_ns) / 1e9

    log = reader._open()
    truth = truth_intervals(log, t0, min_ms)

    report = Auditor(reader).run()
    pred = merge(
        [(f.t_start, f.t_end) for f in report.findings if f.detector == "correlation"],
        slack=0.5,
    )

    matched_truth = sum(1 for g in truth if any(overlaps(g, p, tol) for p in pred))
    matched_pred = sum(1 for p in pred if any(overlaps(p, g, tol) for g in truth))

    notes: list[str] = []
    truth_s = sum(hi - lo for lo, hi in truth)
    if truth and matched_truth < len(truth):
        notes.append(f"{len(truth) - matched_truth} labelled dropouts missed")

    return (
        FlightScore(
            name=path.name,
            duration_s=duration,
            n_truth=len(truth),
            n_pred=len(pred),
            matched_truth=matched_truth,
            matched_pred=matched_pred,
            truth_seconds=truth_s,
            blackout_seconds=truth_s,
        ),
        notes,
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dir", required=True)
    ap.add_argument("--out", default="evals/integrity/REAL_DATA.md")
    ap.add_argument("--min-dropout-ms", type=float, default=200.0,
                    help="ignore dropout marks shorter than this; below ~200ms the "
                         "recorder's own clock resolution dominates")
    ap.add_argument("--tolerance-s", type=float, default=2.0)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--gate", action="store_true",
                    help="exit non-zero if recall falls below --min-recall")
    ap.add_argument("--min-recall", type=float, default=0.80)
    args = ap.parse_args(argv)

    root = Path(args.dir).expanduser()
    files = sorted(p for p in root.iterdir() if p.suffix.lower() == ".ulg")

    # Deduplicate by content: the same flight appears under several UUIDs.
    unique: list[Path] = []
    seen: dict[str, str] = {}
    dupes = 0
    for p in files:
        digest = hashlib.sha1(p.read_bytes()).hexdigest()
        if digest in seen:
            dupes += 1
            continue
        seen[digest] = p.name
        unique.append(p)
    if args.limit:
        unique = unique[: args.limit]

    print(f"{len(files)} files, {dupes} duplicate uploads removed, {len(unique)} distinct flights")

    scores: list[FlightScore] = []
    for i, path in enumerate(unique, 1):
        try:
            score, notes = score_flight(path, args.min_dropout_ms, args.tolerance_s)
        except Exception as exc:  # noqa: BLE001
            print(f"[{i}/{len(unique)}] {path.name}: FAILED {type(exc).__name__}: {exc}")
            traceback.print_exc(file=sys.stderr)
            continue
        scores.append(score)
        print(f"[{i}/{len(unique)}] {path.name[:18]}  truth={score.n_truth:>3} "
              f"pred={score.n_pred:>3} matched={score.matched_truth:>3}  "
              f"{'; '.join(notes)}")

    if not scores:
        print("no flights scored")
        return 1

    tp_recall = sum(s.matched_truth for s in scores)
    n_truth = sum(s.n_truth for s in scores)
    tp_prec = sum(s.matched_pred for s in scores)
    n_pred = sum(s.n_pred for s in scores)

    recall = tp_recall / n_truth if n_truth else 0.0
    precision = tp_prec / n_pred if n_pred else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0

    total_dur = sum(s.duration_s for s in scores)
    total_black = sum(s.blackout_seconds for s in scores)

    lines = [
        "# Detector performance on real flight data",
        "",
        f"Corpus: **{len(scores)} distinct public PX4 flights** from review.px4.io "
        f"({dupes} duplicate uploads removed by content hash), "
        f"{total_dur / 60:.0f} minutes of flight. Generated {date.today()}.",
        "",
        "**Labels are not ours.** PX4's logger writes a dropout record whenever it could "
        "not keep up. That record is the ground truth here — authored by the flight "
        "controller, on hardware we have never touched. Every other precision/recall "
        "number in this repository is scored against faults this repository injected.",
        "",
        f"Matching tolerance ±{args.tolerance_s:.0f}s. Dropout marks shorter than "
        f"{args.min_dropout_ms:.0f} ms are ignored.",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Labelled dropouts | {n_truth} |",
        f"| Correlation findings | {n_pred} |",
        f"| **Recall** | **{recall:.3f}** |",
        f"| **Precision** | **{precision:.3f}** |",
        f"| F1 | {f1:.3f} |",
        f"| Recording time lost to logger stalls | {total_black:.0f}s of "
        f"{total_dur:.0f}s ({100 * total_black / total_dur:.1f}%) |",
        "",
        "## Per flight",
        "",
        "| Flight | Duration | Labelled | Reported | Matched | Stall time |",
        "|---|---|---|---|---|---|",
    ]
    for s in sorted(scores, key=lambda s: -s.blackout_seconds):
        lines.append(
            f"| `{s.name[:18]}` | {s.duration_s:.0f}s | {s.n_truth} | {s.n_pred} | "
            f"{s.matched_truth} | {s.blackout_seconds:.1f}s |"
        )

    lines += [
        "",
        "## What this measures, and what it does not",
        "",
        "It measures whether the correlation detector finds the recorder stalls that the "
        "recorder itself admitted to. It does **not** measure the per-topic gap detector, "
        "whose findings on this corpus are dominated by the same stalls seen once per "
        "topic rather than once per event.",
        "",
        "A missed label is not automatically a detector failure: a dropout the logger "
        "recorded while only low-rate topics were due can pass without leaving a "
        "detectable hole in the arrival stream. The floor on dropout duration exists for "
        "the same reason.",
        "",
    ]

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n")
    print("\n" + "\n".join(lines[:22]))
    print(f"\nwrote {out_path}")

    if args.gate and recall < args.min_recall:
        print(f"\nFAIL: recall {recall:.3f} < {args.min_recall}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
