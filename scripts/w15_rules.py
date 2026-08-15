#!/usr/bin/env python3
"""W15 — settle the D7 `unassessable` question against labels, not against taste.

`nuway_stops` is a shuttle bus that spent its recording parked. Seventy of its 110 topics
are event-driven, and D7 turned one of their silences into a 1,489-second "system-wide
stall" on a 1,492-second file — published as `compromised` at score 0.0. The obvious fix
is to make D7 honour `unassessable` the way D2 and the per-topic scores already do.

That fix was tried four ways and each cost 22+ points of recall against PX4's own dropout
records. The decision could not be made, because one corpus had labels and the other did
not: PX4 said "don't", `nuway_stops` said "do", and there was no way to weigh them.

M1 changed that. `evals/integrity/injected.py` puts exact labels on real ROS 2 recordings
— including `nuway_stops` itself. This script re-runs the same three rules against **both**
labelled corpora and prints one table, so the choice is made on evidence from the platform
that motivated it as well as from the platform that vetoed it.

    uv run python scripts/w15_rules.py --px4 ~/data/public/px4 --injected ~/data/injected

`--quick` scores a subset of PX4 flights, which is a screening tool and not an answer:
numbers measured on a selected subset flatter (W11), and the full corpus is what decides.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: (label, aperiodic_may_create, aperiodic_may_vote)
RULES: tuple[tuple[str, bool, bool], ...] = (
    ("unrestricted (shipped)", True, True),
    ("aperiodic may not create; anyone may vote", False, True),
    ("aperiodic may not create or vote", False, False),
)


def run(cmd: list[str], env: dict[str, str]) -> str:
    proc = subprocess.run(cmd, cwd=ROOT, env={**os.environ, **env, "PYTHONPATH": "."},
                          capture_output=True, text=True)
    if proc.returncode != 0:
        print(proc.stdout[-4000:], file=sys.stderr)
        print(proc.stderr[-4000:], file=sys.stderr)
        raise SystemExit(f"failed: {' '.join(cmd)}")
    return proc.stdout


def parse_metric(markdown: str, name: str) -> float:
    """Pull `| **Recall** | **0.993** |` out of a rendered eval table."""
    for line in markdown.splitlines():
        cells = [c.strip().strip("*") for c in line.split("|")]
        if len(cells) > 2 and cells[1].lower() == name.lower():
            try:
                return float(cells[2])
            except ValueError:
                continue
    return float("nan")


def score_px4(directory: Path, limit: int, env: dict[str, str], out: Path) -> tuple[float, float]:
    run([sys.executable, "-m", "evals.integrity.real_data", "--dir", str(directory),
         "--out", str(out)] + (["--limit", str(limit)] if limit else []), env)
    text = out.read_text()
    return parse_metric(text, "Recall"), parse_metric(text, "Precision")


def score_injected(bags: Path, env: dict[str, str], out: Path) -> tuple[float, float]:
    run([sys.executable, "-m", "evals.integrity.injected", "--bags", str(bags),
         "--out", str(out), "--workers", "4"], env)
    text = out.read_text()
    return parse_metric(text, "Recall"), parse_metric(text, "Precision")


def stops_verdict(source: Path, env: dict[str, str]) -> tuple[str, float, int]:
    """What the audit says about the recording that started all this.

    Deliberately the **full original file**, not the injected corpus's sliced copy of it.
    The artefact W15 describes is one interval growing to 1,489 of 1,492 seconds, and a
    131-second slice cannot produce it — scored against the slice, every rule looks fine
    and the experiment answers a question nobody asked.
    """
    code = (
        "import json;"
        "from baglens.detectors import Auditor;"
        "from baglens.readers import open_bag;"
        f"r=open_bag({str(source)!r});"
        "rep=Auditor(r).run();r.close();"
        "stalls=[f for f in rep.findings if f.detector=='correlation'"
        " and f.summary.startswith('system-wide stall')];"
        "print(json.dumps({'verdict':rep.verdict,'score':rep.overall_score,"
        "'stall_s':round(sum(f.t_end-f.t_start for f in stalls),1),"
        "'stalls':len(stalls),'duration':round(rep.duration_s,1)}))"
    )
    out = run([sys.executable, "-c", code], env)
    data = json.loads(out.strip().splitlines()[-1])
    return data["verdict"], float(data["score"]), int(data["stall_s"])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--px4", default="~/data/public/px4")
    ap.add_argument("--injected", default="~/data/injected")
    ap.add_argument("--source", default="~/data/public/ros2/nuway_stops.mcap",
                    help="the full recording W15 is about, audited whole")
    ap.add_argument("--out", default="evals/integrity/W15_RULES.md")
    ap.add_argument("--quick", type=int, default=0, help="score only N PX4 flights")
    args = ap.parse_args(argv)

    px4 = Path(args.px4).expanduser()
    bags = Path(args.injected).expanduser()
    scratch = ROOT / "evals" / "integrity" / ".w15"
    scratch.mkdir(parents=True, exist_ok=True)

    rows = []
    for i, (label, may_create, may_vote) in enumerate(RULES):
        env = {
            "BAGLENS_APERIODIC_MAY_CREATE": "1" if may_create else "0",
            "BAGLENS_APERIODIC_MAY_VOTE": "1" if may_vote else "0",
        }
        print(f"\n=== {label} ===", flush=True)
        pr, pp = score_px4(px4, args.quick, env, scratch / f"px4_{i}.md")
        print(f"  PX4       recall {pr:.3f}  precision {pp:.3f}", flush=True)
        ir, ip = score_injected(bags, env, scratch / f"inj_{i}.md")
        print(f"  injected  recall {ir:.3f}  precision {ip:.3f}", flush=True)
        verdict, score, stall_s = stops_verdict(Path(args.source).expanduser(), env)
        print(f"  nuway_stops (full file): {verdict} at {score:.1f}, "
              f"{stall_s}s claimed as stall", flush=True)
        rows.append((label, pr, pp, ir, ip, verdict, score, stall_s))

    lines = [
        "# W15 — should D7 honour `unassessable`?",
        "",
        "Three rules, two labelled corpora, one table. The question is whether a topic "
        "with no usable rate model may open a silent interval, and whether it may count "
        "as co-silent inside someone else's.",
        "",
        f"PX4: {'all flights' if not args.quick else f'{args.quick} flights (screening only)'}, "
        "scored against the logger's own dropout records. "
        "Injected: real ROS 2 recordings with exact labels — see `INJECTED.md`.",
        "",
        "| D7 rule | PX4 recall | PX4 precision | Injected recall | Injected precision "
        "| `nuway_stops` verdict | claimed stall |",
        "|---|---|---|---|---|---|---|",
    ]
    for label, pr, pp, ir, ip, verdict, score, stall_s in rows:
        lines.append(f"| {label} | {pr:.3f} | {pp:.3f} | {ir:.3f} | {ip:.3f} | "
                     f"{verdict} at {score:.1f} | {stall_s}s |")
    lines += ["", f"Regenerate: `uv run python scripts/w15_rules.py --px4 {args.px4} "
                  f"--injected {args.injected}`.", ""]

    out = ROOT / args.out
    out.write_text("\n".join(lines) + "\n")
    print("\n" + "\n".join(lines))
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
