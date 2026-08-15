"""Feed a recording through the detectors as if it were arriving live.

This is the on-vehicle path rehearsed on real data. The arrival stream is a real PX4
flight or a real rosbag; only the delivery is simulated, so what is under test is the
thing that will actually run — not a synthetic approximation of it.

    # a real flight at 60x, status once a second
    uv run --extra ulog python scripts/live_feed.py ~/data/public/px4/588ff157-*.ulg \\
        --speed 60 --every 1.0

    # follow a recording that is still being written
    uv run python scripts/live_feed.py /tmp/live.mcap --tail

    # prove the live path agrees with the offline one on this file
    uv run --extra ulog python scripts/live_feed.py FLIGHT.ulg --verify

`--checkpoint` writes the monitor's state as it goes; re-running with the same path
resumes with the baselines already learned instead of spending another warmup window
blind, which is what a vehicle that lost power needs.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from baglens.detectors.auditor import Auditor  # noqa: E402
from baglens.live import LiveMonitor, ReplayFeed, TailFeed  # noqa: E402
from baglens.readers import open_bag  # noqa: E402

BOLD, DIM, RED, YELLOW, GREEN, OFF = (
    "\033[1m", "\033[2m", "\033[31m", "\033[33m", "\033[32m", "\033[0m"
)
COLOUR = {"trustworthy": GREEN, "usable_with_caveats": YELLOW, "compromised": RED}


def _line(report: object, wall: float, n: int) -> str:
    verdict = report.verdict  # type: ignore[attr-defined]
    col = COLOUR.get(verdict, "")
    worst = [f for f in report.findings if f.severity >= 4]  # type: ignore[attr-defined]
    stalls = [f for f in worst if f.detector == "correlation" and f.topic is None]
    head = (
        f"{DIM}t+{wall:6.1f}s{OFF}  {col}{verdict:<20}{OFF}"
        f" score {report.overall_score:5.1f}"  # type: ignore[attr-defined]
        f"  {n:>9,} msgs  {len(report.findings):>3} findings"  # type: ignore[attr-defined]
    )
    if stalls:
        head += f"  {RED}{len(stalls)} stall(s){OFF}"
    return head


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("path")
    ap.add_argument("--speed", type=float, default=60.0,
                    help="replay rate; 1 = real time, 0 = as fast as possible")
    ap.add_argument("--every", type=float, default=1.0, help="seconds between status lines")
    ap.add_argument("--tail", action="store_true", help="follow a file still being written")
    ap.add_argument("--checkpoint", help="path to persist monitor state")
    ap.add_argument("--verify", action="store_true",
                    help="also run the offline audit and compare the two verdicts")
    args = ap.parse_args(argv)

    feed = (
        TailFeed(args.path)
        if args.tail
        else ReplayFeed(args.path, speed=args.speed)
    )
    monitor = LiveMonitor(feed, checkpoint_path=args.checkpoint)

    mode = "tailing" if args.tail else f"replaying at {args.speed or float('inf'):g}x"
    print(f"{BOLD}{mode}{OFF} {args.path}")
    if args.checkpoint and Path(args.checkpoint).exists():
        print(f"{DIM}  resumed from {args.checkpoint} at {monitor.auditor.n:,} messages{OFF}")
    print()

    started = time.monotonic()
    final = None
    for report in monitor.run(snapshot_every_s=args.every, checkpoint_every_s=5.0):
        final = report
        print(_line(report, time.monotonic() - started, monitor.n), flush=True)

    print()
    if final is not None:
        for f in sorted(final.findings, key=lambda f: -f.severity)[:5]:
            print(f"  {RED if f.severity >= 4 else YELLOW}S{f.severity}{OFF} {f.summary}")

    if args.verify:
        offline = Auditor(open_bag(args.path)).run()
        same = (
            final is not None
            and offline.verdict == final.verdict
            and abs(offline.overall_score - final.overall_score) < 1e-9
            and len(offline.findings) == len(final.findings)
        )
        mark = f"{GREEN}IDENTICAL{OFF}" if same else f"{RED}DIVERGED{OFF}"
        print(
            f"\n  offline: {offline.verdict} {offline.overall_score} "
            f"({len(offline.findings)} findings)   live: {final.verdict} "  # type: ignore[union-attr]
            f"{final.overall_score} ({len(final.findings)} findings)   {mark}"  # type: ignore[union-attr]
        )
        return 0 if same else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
