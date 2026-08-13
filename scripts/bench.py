"""Performance gates. Regressions here are the difference between usable and not.

    uv run python scripts/bench.py --bag /tmp/baglens-bags/clean_000.mcap
"""

from __future__ import annotations

import argparse
import json
import resource
import time
from pathlib import Path

TARGETS = {
    "memory_mb": 200.0,  # regardless of file size
    "state_bytes_per_topic": 3300,  # default profile; <2048 under BAGLENS_EDGE_PROFILE=1
    "msgs_per_s": 8000.0,
}


def peak_rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def bench_audit(path: Path) -> dict[str, float]:
    from baglens.detectors import Auditor
    from baglens.readers import open_bag

    reader = open_bag(path)
    t0 = time.perf_counter()
    report = Auditor(reader).run()
    elapsed = time.perf_counter() - t0
    size = path.stat().st_size
    auditor_states = 0
    return {
        "elapsed_s": elapsed,
        "size_mb": size / 1e6,
        "mb_per_s": (size / 1e6) / max(elapsed, 1e-9),
        "messages": float(report.provenance.sample_count),
        "msgs_per_s": report.provenance.sample_count / max(elapsed, 1e-9),
        "topics": float(len(report.topics)),
        "peak_rss_mb": peak_rss_mb(),
        "_": auditor_states,
    }


def bench_state(path: Path) -> dict[str, float]:
    from baglens.detectors import Auditor
    from baglens.readers import open_bag

    auditor = Auditor(open_bag(path))
    auditor.run()
    per_topic = [st.state_bytes() for st in auditor.states.values()]
    return {
        "state_bytes_max": float(max(per_topic) if per_topic else 0),
        "state_bytes_total": float(sum(per_topic)),
        "clock_state_bytes": float(auditor.clock.state_bytes() if auditor.clock else 0),
        "timeline_state_bytes": float(auditor.timeline.state_bytes()),
    }


def bench_scan(path: Path) -> dict[str, float]:
    """Raw arrival-stream throughput: the payload-free hot path with no detectors."""
    from baglens.readers import open_bag

    reader = open_bag(path)
    t0 = time.perf_counter()
    n = sum(1 for _ in reader.arrivals())
    elapsed = time.perf_counter() - t0
    size = path.stat().st_size
    return {
        "scan_elapsed_s": elapsed,
        "scan_msgs_per_s": n / max(elapsed, 1e-9),
        "scan_mb_per_s": (size / 1e6) / max(elapsed, 1e-9),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bag", required=False, default=None)
    ap.add_argument("--generate", action="store_true", help="generate a large bag first")
    ap.add_argument("--duration", type=float, default=600.0)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--gate", action="store_true")
    args = ap.parse_args(argv)

    if args.bag:
        bag = Path(args.bag)
    else:
        from tests.synth.generate import DEFAULT_TOPICS, generate_bag

        bag = Path("/tmp/baglens-bench.mcap")
        if args.generate or not bag.exists():
            print(f"generating a {args.duration:.0f}s bag …")
            generate_bag(bag, seed=7, duration_s=args.duration, topics=DEFAULT_TOPICS)

    results = {**bench_scan(bag), **bench_audit(bag), **bench_state(bag)}
    if args.json:
        print(json.dumps(results, indent=2))
    else:
        print(f"bag              {bag}  ({results['size_mb']:.1f} MB, "
              f"{results['messages']:.0f} msgs, {results['topics']:.0f} topics)")
        print(f"scan only        {results['scan_msgs_per_s']:>10,.0f} msg/s   "
              f"{results['scan_mb_per_s']:>7.1f} MB/s")
        print(f"full audit       {results['msgs_per_s']:>10,.0f} msg/s   "
              f"{results['mb_per_s']:>7.1f} MB/s   {results['elapsed_s']:.2f}s")
        print(f"peak RSS         {results['peak_rss_mb']:>10.1f} MB   (target <{TARGETS['memory_mb']:.0f})")
        print(f"state per topic  {results['state_bytes_max']:>10,.0f} B    "
              f"(target <{TARGETS['state_bytes_per_topic']:,})")
        print(f"clock + timeline {results['clock_state_bytes'] + results['timeline_state_bytes']:>10,.0f} B    (whole-file, not per topic)")

    failures = []
    if results["peak_rss_mb"] > TARGETS["memory_mb"]:
        failures.append("memory")
    if results["state_bytes_max"] > TARGETS["state_bytes_per_topic"]:
        failures.append("state_bytes_per_topic")
    if results["msgs_per_s"] < TARGETS["msgs_per_s"]:
        failures.append("msgs_per_s")
    if failures:
        print("BELOW TARGET: " + ", ".join(failures))
    return 1 if (failures and args.gate) else 0


if __name__ == "__main__":
    raise SystemExit(main())
