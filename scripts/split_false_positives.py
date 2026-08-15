#!/usr/bin/env python3
"""P1.1 — split the false positives by class and by flight.

`evals/integrity/real_data.py` merges **all** `correlation` findings into one interval
list before scoring. That list holds two different claims:

  * `system-wide stall`  (topic is None)   — "the recorder stopped", the claim the tool
                                             actually makes about the recorder, and the
                                             only one a ULog dropout record is evidence
                                             for;
  * `subsystem failure`  (topic is set)    — "a shared driver or bus died", a claim the
                                             dropout labels say nothing about either way.

Merging them also fuses a subsystem finding that happens to overlap a stall into a single
predicted interval, so the published 391 is not even the sum of the two classes. This
script scores each class on its own, against the same labels, and reports where the
unmatched predictions concentrate.

Read-only: it imports the shipped detector and writes one markdown file. No detector
behaviour is changed.

    uv run python scripts/split_false_positives.py --dir ~/data/public/px4
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT))  # `evals` is a package at the repo root, not under src/

from evals.integrity.real_data import merge, overlaps, truth_intervals  # noqa: E402

Interval = tuple[float, float]

#: the classes a `correlation` finding can belong to, in the order they are reported
CLASSES = ("stall", "subsystem", "combined")


@dataclass
class Split:
    """One flight, scored once per class."""

    name: str
    duration_s: float
    n_truth: int
    truth_seconds: float
    n_pred: dict[str, int] = field(default_factory=dict)
    matched_pred: dict[str, int] = field(default_factory=dict)
    matched_truth: dict[str, int] = field(default_factory=dict)


def score_flight(path: Path, min_ms: float, tol: float) -> Split | None:
    from baglens.detectors.auditor import Auditor
    from baglens.readers.ulog_reader import UlogReader

    try:
        reader = UlogReader(path)
        meta = reader.metadata()
        t0 = meta.start_time_ns / 1e9
        duration = (meta.end_time_ns - meta.start_time_ns) / 1e9
        truth = truth_intervals(reader._open(), t0, min_ms)
        findings = [f for f in Auditor(reader).run().findings if f.detector == "correlation"]
    except Exception as exc:  # noqa: BLE001 - one bad file must not lose the corpus
        print(f"{path.name}: FAILED {type(exc).__name__}: {exc}", file=sys.stderr)
        return None

    # `topic is None` is what `finalize` uses to distinguish the two claims; the summary
    # prefix is checked too so a future third finding kind cannot silently join a bucket.
    raw: dict[str, list[Interval]] = {
        "stall": [(f.t_start, f.t_end) for f in findings
                  if f.topic is None and f.summary.startswith("system-wide stall")],
        "subsystem": [(f.t_start, f.t_end) for f in findings
                      if f.topic is not None and f.summary.startswith("subsystem failure")],
    }
    unknown = len(findings) - len(raw["stall"]) - len(raw["subsystem"])
    if unknown:
        print(f"{path.name}: {unknown} correlation findings in neither class", file=sys.stderr)
    raw["combined"] = raw["stall"] + raw["subsystem"]  # what the published eval scores

    out = Split(path.name, duration, len(truth), sum(hi - lo for lo, hi in truth))
    for cls in CLASSES:
        pred = merge(raw[cls], slack=0.5)
        out.n_pred[cls] = len(pred)
        out.matched_pred[cls] = sum(1 for p in pred if any(overlaps(p, g, tol) for g in truth))
        out.matched_truth[cls] = sum(1 for g in truth if any(overlaps(g, p, tol) for p in pred))
    return out


def _job(args: tuple[Path, float, float]) -> Split | None:
    return score_flight(*args)


def distinct(root: Path) -> tuple[list[Path], int]:
    """Deduplicate by content hash — review.px4.io serves one flight under many UUIDs."""
    seen: set[str] = set()
    unique: list[Path] = []
    dupes = 0
    for p in sorted(root.iterdir()):
        if p.suffix.lower() != ".ulg":
            continue
        digest = hashlib.sha1(p.read_bytes()).hexdigest()
        if digest in seen:
            dupes += 1
            continue
        seen.add(digest)
        unique.append(p)
    return unique, dupes


def rate(num: int, den: int) -> float:
    return num / den if den else 0.0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dir", required=True)
    ap.add_argument("--out", default="evals/integrity/FP_SPLIT.md")
    ap.add_argument("--min-dropout-ms", type=float, default=200.0)
    ap.add_argument("--tolerance-s", type=float, default=2.0)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 4) - 1))
    args = ap.parse_args(argv)

    unique, dupes = distinct(Path(args.dir).expanduser())
    if args.limit:
        unique = unique[: args.limit]
    print(f"{len(unique)} distinct flights ({dupes} duplicate uploads removed), "
          f"{args.workers} workers")

    jobs = [(p, args.min_dropout_ms, args.tolerance_s) for p in unique]
    scores: list[Split] = []
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        for i, s in enumerate(pool.map(_job, jobs), 1):
            if s is None:
                continue
            scores.append(s)
            print(f"[{i}/{len(jobs)}] {s.name[:18]}  truth={s.n_truth:>3} "
                  f"stall={s.n_pred['stall']:>3} sub={s.n_pred['subsystem']:>3} "
                  f"comb={s.n_pred['combined']:>3} "
                  f"matched={s.matched_pred['combined']:>3}")

    if not scores:
        print("no flights scored")
        return 1

    n_truth = sum(s.n_truth for s in scores)
    total_dur = sum(s.duration_s for s in scores)
    tot: dict[str, dict[str, int]] = {
        cls: {
            "pred": sum(s.n_pred[cls] for s in scores),
            "tp": sum(s.matched_pred[cls] for s in scores),
            "rec": sum(s.matched_truth[cls] for s in scores),
        }
        for cls in CLASSES
    }

    lines = [
        "# P1.1 — the 239 unmatched findings, split by class",
        "",
        f"Corpus: **{len(scores)} distinct public PX4 flights**, {total_dur / 60:.0f} minutes, "
        f"{n_truth} labelled dropouts. Matching tolerance ±{args.tolerance_s:.0f}s; dropout "
        f"marks under {args.min_dropout_ms:.0f} ms ignored. Generated {date.today()}.",
        "",
        "The published precision scores every `correlation` finding against the ULog dropout "
        "labels. But `correlation` emits two different claims, and only one of them is a "
        "claim about the recorder:",
        "",
        "| Class | Claim | Is a dropout label evidence for it? |",
        "|---|---|---|",
        "| `system-wide stall` | the recorder, disk, CPU or power stopped | yes — this is the same event |",
        "| `subsystem failure` | a shared driver or bus died | no — the label is silent either way |",
        "",
        "## Scored separately against the same labels",
        "",
        "| Class | Findings | Matched | Precision | Labels found | Recall |",
        "|---|---|---|---|---|---|",
    ]
    label = {"stall": "`system-wide stall` only", "subsystem": "`subsystem failure` only",
             "combined": "**both (what is published today)**"}
    for cls in CLASSES:
        t = tot[cls]
        lines.append(
            f"| {label[cls]} | {t['pred']} | {t['tp']} | {rate(t['tp'], t['pred']):.3f} | "
            f"{t['rec']}/{n_truth} | {rate(t['rec'], n_truth):.3f} |"
        )
    fused = tot["stall"]["pred"] + tot["subsystem"]["pred"] - tot["combined"]["pred"]
    lines += [
        "",
        f"The two classes do not sum to the combined row: {fused} predicted intervals are "
        "produced by merging a subsystem finding into an overlapping stall, so the "
        "published denominator is not a count of either claim.",
        "",
        "## Where the unmatched findings concentrate",
        "",
        "| Flight | Duration | Labelled | Stall | ✗ | Subsystem | ✗ |",
        "|---|---|---|---|---|---|---|",
    ]
    worst = sorted(
        scores,
        key=lambda s: -((s.n_pred["stall"] - s.matched_pred["stall"])
                        + (s.n_pred["subsystem"] - s.matched_pred["subsystem"])),
    )
    for s in worst[:25]:
        lines.append(
            f"| `{s.name[:18]}` | {s.duration_s:.0f}s | {s.n_truth} | "
            f"{s.n_pred['stall']} | {s.n_pred['stall'] - s.matched_pred['stall']} | "
            f"{s.n_pred['subsystem']} | {s.n_pred['subsystem'] - s.matched_pred['subsystem']} |"
        )

    zero = [s for s in scores if s.n_truth == 0]
    zero_fp = sum(s.n_pred["combined"] - s.matched_pred["combined"] for s in zero)
    all_fp = sum(s.n_pred["combined"] - s.matched_pred["combined"] for s in scores)
    lines += [
        "",
        f"{len(zero)} of {len(scores)} flights carry no dropout label at all and account for "
        f"{zero_fp} of the {all_fp} unmatched findings "
        f"({100 * rate(zero_fp, all_fp):.0f}%). On those flights every finding is unmatched "
        "by construction, so they set precision without the labels ever being able to "
        "confirm one.",
        "",
    ]

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n")
    print("\n" + "\n".join(lines))
    print(f"\nwrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
