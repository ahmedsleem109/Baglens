#!/usr/bin/env python3
"""Fail a build when the recordings it produced got worse.

A different buyer for the same engine. The detector evals gate *this* repository's
detectors; this gates *someone else's* recordings — a platform team whose simulation or
integration job writes bags, and who would like a pull request that quietly starts
dropping IMU messages to fail rather than to merge.

It is deliberately a single process with an exit code and a JSON report, because that is
the interface CI actually has. Thresholds are arguments rather than a config file for the
same reason: the diff that changes the bar should be the one a reviewer reads.

    uv run python scripts/quality_gate.py --path recordings/ --min-score 85
    uv run python scripts/quality_gate.py --path out.mcap --max-verdict usable_with_caveats

Comparing against a baseline is the more useful mode once a project has one: a recording
that has always scored 72 is not a regression, and a gate that cannot tell the difference
gets its threshold lowered until it means nothing.

    uv run python scripts/quality_gate.py --path recordings/ --baseline baseline.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from baglens.catalog.indexer import BAG_GLOBS  # noqa: E402
from baglens.detectors import Auditor  # noqa: E402
from baglens.readers import open_bag  # noqa: E402

#: worst to best; `--max-verdict` names the worst that still passes
VERDICTS = ["compromised", "usable_with_caveats", "trustworthy"]

#: `unassessable` is not on that ladder, because it is a refusal to grade rather than a
#: grade. A gate cannot pass a recording nobody could assess — that is how an unaudited
#: recording gets through wearing a clean label — so it is ranked below the worst grade
#: for comparison purposes and reported by name.
UNASSESSABLE = "unassessable"


def rank(verdict: str) -> int:
    return VERDICTS.index(verdict) if verdict in VERDICTS else -1


def discover(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    found: list[Path] = []
    for pattern in BAG_GLOBS:
        found.extend(path.rglob(pattern))
    return sorted({p for p in found if p.is_file()})


def audit_one(path: Path) -> dict[str, object]:
    reader = open_bag(path)
    try:
        report = Auditor(reader).run()
    finally:
        reader.close()
    worst = sorted(report.findings, key=lambda f: (-int(f.severity), f.t_start))
    return {
        "path": str(path),
        "score": round(report.overall_score, 2),
        "verdict": report.verdict,
        "duration_s": round(report.duration_s, 2),
        "findings": len(report.findings),
        "worst": [f.summary for f in worst[:3]],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--path", required=True, help="a recording, or a directory of them")
    ap.add_argument("--min-score", type=float, default=None)
    ap.add_argument("--max-verdict", choices=VERDICTS, default=None,
                    help="the worst verdict that still passes")
    ap.add_argument("--baseline", type=Path, default=None,
                    help="a previous report; fail on a drop rather than on an absolute")
    ap.add_argument("--max-drop", type=float, default=5.0,
                    help="score points a recording may lose against the baseline")
    ap.add_argument("--out", type=Path, default=None, help="write the JSON report here")
    args = ap.parse_args(argv)

    paths = discover(Path(args.path).expanduser())
    if not paths:
        print(f"no recordings found under {args.path}")
        return 1

    results = []
    for p in paths:
        try:
            results.append(audit_one(p))
        except Exception as exc:  # noqa: BLE001 - an unreadable recording is a failure, not a crash
            results.append({"path": str(p), "error": f"{type(exc).__name__}: {exc}",
                            "score": 0.0, "verdict": "compromised", "findings": 0,
                            "worst": []})

    baseline: dict[str, float] = {}
    if args.baseline and args.baseline.exists():
        prior = json.loads(args.baseline.read_text())
        baseline = {Path(r["path"]).name: float(r["score"]) for r in prior.get("results", [])}

    failures: list[str] = []
    for r in results:
        name = Path(str(r["path"])).name
        score = float(r["score"])  # type: ignore[arg-type]
        if r.get("error"):
            failures.append(f"{name}: {r['error']}")
            continue
        if str(r["verdict"]) == UNASSESSABLE:
            failures.append(
                f"{name}: {UNASSESSABLE} — too little of this recording could be measured "
                f"to say whether it got worse"
            )
            continue
        if args.min_score is not None and score < args.min_score:
            failures.append(f"{name}: score {score:.1f} below {args.min_score:.1f}")
        if (args.max_verdict is not None
                and rank(str(r["verdict"])) < rank(args.max_verdict)):
            failures.append(f"{name}: verdict {r['verdict']} worse than {args.max_verdict}")
        if name in baseline and baseline[name] - score > args.max_drop:
            failures.append(
                f"{name}: score fell {baseline[name] - score:.1f} points "
                f"({baseline[name]:.1f} -> {score:.1f})"
            )

    report = {"results": results, "failures": failures, "passed": not failures}
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2))

    for r in results:
        mark = "FAIL" if r.get("error") else f"{float(r['score']):5.1f}"  # type: ignore[arg-type]
        print(f"{mark}  {str(r['verdict']):<20} {Path(str(r['path'])).name}"
              + (f"  — {r['worst'][0]}" if r.get("worst") else ""))  # type: ignore[index]
    if failures:
        print("\nrecording quality gate FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"\n{len(results)} recording(s) passed the quality gate")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
