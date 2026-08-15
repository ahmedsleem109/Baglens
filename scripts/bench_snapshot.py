#!/usr/bin/env python3
"""What a live snapshot actually costs, and where the time goes.

The published figure was ~14% at 1 Hz. It was wrong: measured on a real 311 s PX4 flight
at 2.7 kHz, snapshots at 1 Hz more than doubled the run. This script is here so the
number is checkable rather than asserted, and so a change that makes snapshots slower
again shows up as a number instead of as a complaint from a vehicle.

    uv run python scripts/bench_snapshot.py --path ~/data/public/px4/588ff157*.ulg
"""

from __future__ import annotations

import argparse
import cProfile
import glob
import io
import pstats
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from baglens.detectors.auditor import Auditor  # noqa: E402
from baglens.live import LiveMonitor, ReplayFeed  # noqa: E402
from baglens.readers import open_bag  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--path", required=True, help="a recording; globs are expanded")
    ap.add_argument("--hz", type=float, default=1.0, help="snapshot rate to simulate")
    ap.add_argument("--profile", action="store_true", help="print the hot functions")
    args = ap.parse_args(argv)

    matches = sorted(glob.glob(str(Path(args.path).expanduser())))
    if not matches:
        print(f"no recording matched {args.path}")
        return 1
    path = Path(matches[0])

    n = sum(1 for _ in open_bag(path).arrivals())
    duration = open_bag(path).metadata().duration_s
    every = max(1, int(n / duration / args.hz)) if duration else 1000
    print(f"{path.name}: {n} arrivals over {duration:.0f}s "
          f"({n / duration:.0f}/s); snapshot every {every} arrivals")

    t0 = time.perf_counter()
    list(LiveMonitor(ReplayFeed(path, speed=0)).run())
    base = time.perf_counter() - t0

    t0 = time.perf_counter()
    snaps = list(LiveMonitor(ReplayFeed(path, speed=0)).run(snapshot_every_n=every))
    with_snapshots = time.perf_counter() - t0

    print(f"audit only               {base:6.2f}s")
    print(f"+ {len(snaps):4d} snapshots @ {args.hz}Hz  {with_snapshots:6.2f}s "
          f"({100 * (with_snapshots - base) / base:+.0f}%)")

    auditor = Auditor(open_bag(path))
    auditor._ensure_global_detectors()
    for arrival in open_bag(path).arrivals():
        auditor.push(arrival)

    def timed(fn, reps: int = 5) -> float:
        start = time.perf_counter()
        for _ in range(reps):
            fn()
        return (time.perf_counter() - start) / reps * 1000

    state = auditor.to_state()
    print(f"\nto_state                 {timed(auditor.to_state):6.1f}ms")
    print(f"from_state               "
          f"{timed(lambda: Auditor.from_state(state, auditor.reader, auditor.cfg)):6.1f}ms")
    print(f"one snapshot (both+finish) "
          f"{timed(lambda: Auditor.from_state(auditor.to_state(), auditor.reader, auditor.cfg).finish()):6.1f}ms")

    if args.profile:
        pr = cProfile.Profile()
        pr.enable()
        for _ in range(3):
            Auditor.from_state(auditor.to_state(), auditor.reader, auditor.cfg).finish()
        pr.disable()
        out = io.StringIO()
        pstats.Stats(pr, stream=out).sort_stats("cumulative").print_stats(15)
        print(out.getvalue())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
