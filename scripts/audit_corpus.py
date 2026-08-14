#!/usr/bin/env python3
"""Audit a directory of real recordings and rank them by health score.

This is the Tier 0.1 instrument: the precision/recall table in this repository is
produced by a generator that also lives in this repository, so it can only prove the
detectors and the generator agree. Running the same detectors over recordings made by
people who have never heard of baglens is the only thing that turns that table into a
claim about the world.

Emits one JSON row per recording plus a per-topic table, so findings can be confirmed
or dismissed by hand afterwards.

Note on the streaming constraint: this script is *analysis*, not a detector. It holds
one recording's report in memory at a time and nothing across recordings.

Usage:
    python scripts/audit_corpus.py --dir ~/data/public/px4 --out ~/data/corpus_audit.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from baglens.detectors.auditor import Auditor  # noqa: E402
from baglens.readers import open_bag  # noqa: E402

SUFFIXES = (".ulg", ".mcap", ".bag")


def audit_one(path: Path) -> dict[str, Any]:
    t0 = time.time()
    reader = open_bag(path)
    report = Auditor(reader).run()
    elapsed = time.time() - t0

    duration = max(report.duration_s, 1e-9)
    topics = []
    for th in report.topics:
        actual_hz = th.count / duration
        expected = th.expected_hz or 0.0
        # How far the learned baseline is from the rate the topic actually sustained.
        # A ratio far above 1 means the baseline describes something the topic never
        # did — the signature of a cadence learned from a burst rather than a cadence.
        ratio = (expected / actual_hz) if actual_hz > 0 else 0.0
        topics.append({
            "topic": th.topic,
            "count": th.count,
            "expected_hz": round(expected, 4),
            "actual_hz": round(actual_hz, 4),
            "baseline_ratio": round(ratio, 3),
            "gap_count": th.gap_count,
            "max_gap_s": th.max_gap_s,
            "estimated_dropped": th.estimated_dropped,
            "jitter_cv": th.jitter_cv,
            "score": th.score,
        })

    findings = [{
        "detector": f.detector,
        "severity": int(f.severity),
        "severity_name": f.severity.name,
        "topic": f.topic,
        "t_start": round(f.t_start, 3),
        "t_end": round(f.t_end, 3),
        "summary": f.summary,
        "evidence": dict(f.evidence or {}),
    } for f in report.findings]

    return {
        "path": str(path),
        "name": path.name,
        "ok": True,
        "audit_seconds": round(elapsed, 2),
        "size_bytes": path.stat().st_size,
        "duration_s": report.duration_s,
        "overall_score": report.overall_score,
        "verdict": report.verdict,
        "n_topics": len(report.topics),
        "n_findings": len(findings),
        "by_severity": dict(Counter(f["severity_name"] for f in findings)),
        "by_detector": dict(Counter(f["detector"] for f in findings)),
        "n_caveats": len(report.caveats),
        "topics": topics,
        "findings": findings,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dir", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    root = Path(args.dir).expanduser()
    files = sorted(p for p in root.iterdir() if p.suffix.lower() in SUFFIXES)
    if args.limit:
        files = files[: args.limit]
    print(f"{len(files)} recordings in {root}")

    out_path = Path(args.out).expanduser()
    rows: list[dict[str, Any]] = []
    for i, path in enumerate(files, 1):
        try:
            row = audit_one(path)
            print(f"[{i}/{len(files)}] {path.name}  score={row['overall_score']:>5.1f}  "
                  f"{row['verdict']:<22} findings={row['n_findings']:>5}  "
                  f"topics={row['n_topics']:>3}  {row['audit_seconds']:>5.1f}s")
        except Exception as exc:  # noqa: BLE001 - one bad file must not lose the corpus
            print(f"[{i}/{len(files)}] {path.name}  FAILED: {type(exc).__name__}: {exc}")
            traceback.print_exc(file=sys.stderr)
            row = {"path": str(path), "name": path.name, "ok": False,
                   "error": f"{type(exc).__name__}: {exc}"}
        rows.append(row)
        out_path.write_text(json.dumps(rows, indent=2))

    good = [r for r in rows if r.get("ok")]
    print(f"\naudited {len(good)}/{len(rows)}")
    if good:
        print("\nworst 20 by health score:")
        for r in sorted(good, key=lambda r: r["overall_score"])[:20]:
            print(f"  {r['overall_score']:>5.1f}  {r['verdict']:<22} {r['name']}")
    print(f"\nwrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
