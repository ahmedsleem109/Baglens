#!/usr/bin/env python3
"""Reproduce the stall-attribution study in `evals/integrity/STALL_ATTRIBUTION.md`.

Runs the shipped kernel over a corpus of real PX4 flights and reports the corpus-level
verdict distribution. The result that matters is a negative one — on public flight data
nothing in the log explains the recorder stalls — so this exists to let anyone check it
rather than take the claim on trust.

    uv sync --extra ulog
    uv run python scripts/study_stall_attribution.py --dir ~/data/public/px4

Fetch the corpus first with `scripts/fetch_px4.py`.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from baglens.kernels.attribution import StallAttributor  # noqa: E402

#: PX4 names. A ROS 2 corpus simply misses them and reports `no_data`, which is the
#: correct answer rather than a failure.
CANDIDATES = [
    ("cpuload", "load"),
    ("cpuload", "ram_usage"),
    ("system_power", "voltage5v_v"),
    ("battery_status", "current_a"),
    ("battery_status", "voltage_v"),
]
#: an interval this long with *no* topic publishing is not a sensor fault
BLACKOUT_S = 0.1


def analyse(path: Path) -> dict[str, Any] | None:
    import numpy as np
    from pyulog import ULog

    try:
        log = ULog(str(path))
    except Exception:  # noqa: BLE001 - one bad file must not lose the corpus
        return None

    series: dict[str, Any] = {}
    allts: list[Any] = []
    for d in log.data_list:
        key = d.name + (f"_{d.multi_id}" if d.multi_id else "")
        ts = np.asarray(d.data["timestamp"], dtype=np.float64) / 1e6
        if len(ts):
            series[key] = d
            allts.append(ts)
    if not allts:
        return None

    merged = np.sort(np.concatenate(allts))
    t0 = merged[0]
    rel = merged - t0
    span = float(rel[-1])
    dt = np.diff(merged)
    idx = np.where(dt > BLACKOUT_S)[0]
    if len(idx) < 4 or span < 60:
        return None

    stalls = [(float(rel[i]), float(rel[i + 1])) for i in idx]
    attributor = StallAttributor(stalls, duration_s=span)

    for topic, field_name in CANDIDATES:
        d = series.get(topic)
        if d is None or field_name not in d.data:
            continue
        ts = np.asarray(d.data["timestamp"], dtype=np.float64) / 1e6 - t0
        vals = np.asarray(d.data[field_name], dtype=np.float64)
        # strict: a timestamp column shorter than its value column would mean a
        # malformed dataset, and silently truncating it would skew the study.
        for t, v in zip(ts, vals, strict=True):
            attributor.feed(f"{topic}.{field_name}", float(t), float(v))

    rep = attributor.report()
    top = rep.attributions[0] if rep.attributions else None
    return {
        "name": path.name[:18],
        "verdict": rep.verdict,
        "kind": rep.pattern.kind,
        "dispersion": rep.pattern.dispersion,
        "lost_frac": float(dt[idx].sum()) / span,
        "top_signal": top.signal if top else None,
        "top_effect": top.effect_size if top else None,
        "interpretation": rep.interpretation,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dir", required=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=10)
    args = ap.parse_args()

    files = sorted(Path(args.dir).expanduser().glob("*.ulg"))
    if args.limit:
        files = files[: args.limit]
    if not files:
        print(f"no .ulg files in {args.dir} — fetch some with scripts/fetch_px4.py")
        return 1

    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        rows = [r for r in pool.map(analyse, files) if r]

    if not rows:
        print("no flight had enough stalls to study")
        return 1

    print(f"{len(rows)} flights studied\n")
    print("verdicts:", dict(Counter(r["verdict"] for r in rows)))
    print("patterns:", dict(Counter(r["kind"] for r in rows)))

    disp = [r["dispersion"] for r in rows if r["dispersion"]]
    if disp:
        print(f"dispersion: mean {sum(disp) / len(disp):.2f} (1.0 = random/Poisson)")

    lost = [r["lost_frac"] for r in rows]
    print(f"recording time lost: mean {sum(lost) / len(lost) * 100:.1f}%")

    attributed = [r for r in rows if r["verdict"] == "attributed"]
    print(f"\nflights where a signal explains the stalls: {len(attributed)}/{len(rows)}")
    for r in attributed:
        print(f"  {r['name']}: {r['top_signal']} d={r['top_effect']:+.2f}")
    if len({r["top_signal"] for r in attributed}) > 1 or len(attributed) < 0.1 * len(rows):
        print("  (few, and inconsistent between flights — consistent with chance)")

    for r in rows:
        if r["verdict"] == "unexplained":
            print(f"\nrepresentative:\n  {r['name']}: {r['interpretation']}")
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
