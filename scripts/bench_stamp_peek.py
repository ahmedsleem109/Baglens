"""What does reading `header.stamp` cost the audit?

The audit is payload-free, and that is why it sustains its published throughput. F1 is the
first feature that reads into the payload, so the constraint is explicit: peek at a fixed
offset, do not deserialize, and **measure the cost before and after**. This is that
measurement.

Three configurations over the same recording:

* `payload-free`  — the audit as it was: timing records only, no stamps read
* `peek`          — the same audit with `want_stamps`, so every headered message costs one
                    struct.unpack
* `+data_age`     — peek plus the F1 detector actually consuming the stamps

    uv run python scripts/bench_stamp_peek.py ~/data/public/ros2/nuway_stops.mcap
    uv run python scripts/bench_stamp_peek.py ~/data/public/ros2 --repeat 3
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from baglens.detectors.auditor import ALL_DETECTORS, Auditor
from baglens.readers.base import open_bag

WITHOUT_AGE = [d for d in ALL_DETECTORS if d != "data_age"]


def _time(fn, repeat: int) -> float:
    """Best of N. The fastest run is the one least polluted by other load."""
    best = float("inf")
    for _ in range(repeat):
        t0 = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - t0)
    return best


def bench_one(path: Path, repeat: int) -> dict[str, float]:
    def arrivals_only(stamps: bool) -> int:
        reader = open_bag(path)
        reader.want_stamps = stamps
        return sum(1 for _ in reader.arrivals())

    n = arrivals_only(False)

    plain = _time(lambda: arrivals_only(False), repeat)
    peek = _time(lambda: arrivals_only(True), repeat)

    def audit(detectors: list[str] | None) -> None:
        Auditor(open_bag(path), detectors=detectors).run()

    audit_plain = _time(lambda: audit(WITHOUT_AGE), repeat)
    audit_age = _time(lambda: audit(None), repeat)

    return {
        "messages": float(n),
        "read_plain_s": plain,
        "read_peek_s": peek,
        "audit_plain_s": audit_plain,
        "audit_age_s": audit_age,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("target", type=Path, help="a recording, or a directory of them")
    ap.add_argument("--repeat", type=int, default=2, help="best of N (default 2)")
    args = ap.parse_args(argv)

    files = [args.target] if args.target.is_file() else sorted(args.target.glob("*.mcap"))
    rows = []
    for path in files:
        try:
            r = bench_one(path, args.repeat)
        except Exception as exc:  # a corpus is allowed to contain an unreadable file
            print(f"{path.name}: skipped ({exc})")
            continue
        r["name"] = path.name  # type: ignore[assignment]
        rows.append(r)

        n = r["messages"]
        print(
            f"\n{path.name}  ({n:,.0f} messages)\n"
            f"  read, payload-free   {n / r['read_plain_s']:>12,.0f} msg/s\n"
            f"  read, with peek      {n / r['read_peek_s']:>12,.0f} msg/s"
            f"   ({r['read_peek_s'] / r['read_plain_s'] - 1:+.1%})\n"
            f"  audit, no data_age   {n / r['audit_plain_s']:>12,.0f} msg/s\n"
            f"  audit, with data_age {n / r['audit_age_s']:>12,.0f} msg/s"
            f"   ({r['audit_age_s'] / r['audit_plain_s'] - 1:+.1%})"
        )

    if len(rows) > 1:
        tot = sum(r["messages"] for r in rows)
        for label, plain_k, peek_k in (
            ("read", "read_plain_s", "read_peek_s"),
            ("audit", "audit_plain_s", "audit_age_s"),
        ):
            a = sum(r[plain_k] for r in rows)
            b = sum(r[peek_k] for r in rows)
            print(f"\ncorpus {label}: {tot / a:,.0f} -> {tot / b:,.0f} msg/s ({b / a - 1:+.1%})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
