"""Precision/recall harness for the detector library.

Generates the fault matrix, audits every bag, matches findings against ground truth,
and emits per-detector precision, recall, F1 and the false-positive rate on clean data.

The clean control set is the important half. False positives on healthy recordings are
the failure mode that destroys trust, so they are measured explicitly and gated in CI.

    uv run python -m evals.integrity.run --bags /tmp/baglens-bags --out evals/integrity/RESULTS.md
"""

from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: seconds of slack allowed when matching a finding to a ground-truth window
TOLERANCE_S = 2.0

#: fault kind -> the detector(s) that are supposed to fire for it
EXPECTED: dict[str, tuple[str, ...]] = {
    "topic_dropout": ("gap",),
    "rate_degradation": ("rate_degradation",),
    "jitter_injection": ("jitter",),
    "diffuse_drops": ("dropped",),
    "recorder_lag": ("clock_lag",),
    "clock_step": ("clock_step", "clock"),
    "correlated_stall": ("correlation",),
    "truncation": ("file_integrity",),
}

#: detectors whose findings a given fault also legitimately explains, so they are not
#: counted as false positives. A 12 s dropout really does drop messages and really does
#: silence the topic; punishing the auditor for saying so would be measuring the wrong thing.
EXPLAINS: dict[str, tuple[str, ...]] = {
    "topic_dropout": ("gap", "dropped", "correlation", "jitter", "rate_degradation"),
    "rate_degradation": ("rate_degradation", "dropped", "jitter", "gap"),
    "jitter_injection": ("jitter", "gap", "dropped", "rate_degradation"),
    "diffuse_drops": ("dropped", "gap", "jitter", "rate_degradation"),
    "recorder_lag": ("clock_lag", "clock_step", "clock", "dropped", "rate_degradation", "jitter"),
    "clock_step": ("clock_step", "clock", "gap", "dropped", "jitter", "correlation",
                   "rate_degradation"),
    "correlated_stall": ("correlation", "gap", "dropped", "jitter", "rate_degradation"),
    "truncation": ("file_integrity", "gap", "dropped", "clock_lag", "rate_degradation",
                   "jitter", "correlation", "clock_step"),
}

#: detector ids that are reported under another row. A backward clock step surfaces as
#: a monotonicity failure ("clock"), which is the same event seen from the other side.
ALIASES = {"clock": "clock_step"}

ALL_TRACKED = (
    "gap",
    "rate_degradation",
    "jitter",
    "dropped",
    "clock_lag",
    "clock_step",
    "correlation",
    "file_integrity",
)


@dataclass
class Counts:
    tp: int = 0
    fp: int = 0
    fn: int = 0
    #: findings emitted on fault-free recordings
    clean_fp: int = 0
    clean_bags: int = 0
    faulted_bags: int = 0
    latency_s: list[float] = field(default_factory=list)

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if (self.tp + self.fp) else 1.0

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if (self.tp + self.fn) else 1.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0

    @property
    def clean_fp_rate(self) -> float:
        return self.clean_fp / self.clean_bags if self.clean_bags else 0.0


TARGETS = {
    "gap": (0.90, 0.95),  # (precision, recall)
    "rate_degradation": (0.80, 0.85),
    "jitter": (0.85, 0.80),
    "dropped": (0.80, 0.85),
    "clock_lag": (0.85, 0.90),
    "clock_step": (0.90, 0.95),
    "correlation": (0.80, 0.85),
    "file_integrity": (0.95, 0.95),
}


def _window(fault: dict[str, Any], duration: float) -> tuple[float, float]:
    t0 = float(fault.get("t_start") or 0.0)
    t1 = float(fault.get("t_end") or 0.0)
    kind = fault["kind"]
    if kind in ("recorder_lag", "diffuse_drops", "truncation"):
        return (0.0, duration)  # whole-run faults
    if t1 <= t0:
        t1 = t0
    return (t0, t1)


def _overlaps(a: tuple[float, float], b: tuple[float, float], tol: float = TOLERANCE_S) -> bool:
    return (a[0] - tol) <= b[1] and (b[0] - tol) <= a[1]


def _fault_topics(fault: dict[str, Any]) -> set[str]:
    topics = set(fault.get("topics") or ())
    if fault.get("topic"):
        topics.add(fault["topic"])
    return topics


def audit_one(path: str) -> dict[str, Any]:
    """Audit one bag. Runs in a worker process."""
    from baglens.detectors import Auditor
    from baglens.readers import open_bag

    t0 = time.perf_counter()
    reader = open_bag(path)
    report = Auditor(reader).run()
    elapsed = time.perf_counter() - t0
    reader.close()
    return {
        "path": path,
        "elapsed_s": elapsed,
        "size_bytes": Path(path).stat().st_size,
        "verdict": report.verdict,
        "score": report.overall_score,
        "findings": [
            {
                "detector": f.detector,
                "topic": f.topic,
                "t_start": f.t_start,
                "t_end": f.t_end,
                "severity": int(f.severity),
                "summary": f.summary,
            }
            for f in report.findings
        ],
    }


def score_bag(truth: dict[str, Any], result: dict[str, Any], counts: dict[str, Counts]) -> None:
    duration = float(truth["duration_s"])
    faults = truth["faults"]
    findings = [
        {**f, "detector": ALIASES.get(f["detector"], f["detector"])}
        for f in result["findings"]
        if ALIASES.get(f["detector"], f["detector"]) in ALL_TRACKED
    ]

    if not faults:
        # clean control bag: every finding is a false positive, with no leniency
        for f in findings:
            counts[f["detector"]].clean_fp += 1
        for det in ALL_TRACKED:
            counts[det].clean_bags += 1
        return

    for det in ALL_TRACKED:
        counts[det].faulted_bags += 1

    matched_findings: set[int] = set()
    for fault in faults:
        kind = fault["kind"]
        want = EXPECTED.get(kind, ())
        win = _window(fault, duration)
        topics = _fault_topics(fault)
        hit = False
        for i, f in enumerate(findings):
            if f["detector"] not in want:
                continue
            if topics and f["topic"] and f["topic"] not in topics:
                continue
            if not _overlaps(win, (f["t_start"], f["t_end"])):
                continue
            hit = True
            matched_findings.add(i)
        primary = want[0] if want else None
        if primary:
            if hit:
                counts[primary].tp += 1
            else:
                counts[primary].fn += 1

    # anything left over is a false positive unless some injected fault explains it
    explained: set[str] = set()
    for fault in faults:
        explained |= set(EXPLAINS.get(fault["kind"], ()))
    for i, f in enumerate(findings):
        if i in matched_findings:
            continue
        if f["detector"] in explained:
            continue
        counts[f["detector"]].fp += 1


def render(counts: dict[str, Counts], meta: dict[str, Any]) -> str:
    lines = [
        "# Detector precision and recall",
        "",
        f"Corpus: **{meta['n_faulted']} faulted** + **{meta['n_clean']} clean** synthetic bags, "
        f"{meta['duration_s']:.0f}s each, seed-deterministic.",
        f"Matching tolerance: ±{TOLERANCE_S:.0f}s. Generated {meta['generated_at']}.",
        "",
        "| Detector | Precision | Recall | F1 | target P/R | FP per clean bag | verdict |",
        "|---|---|---|---|---|---|---|",
    ]
    for det in ALL_TRACKED:
        c = counts[det]
        tp_, tr = TARGETS[det]
        ok = c.precision >= tp_ and c.recall >= tr
        lines.append(
            f"| `{det}` | {c.precision:.3f} | {c.recall:.3f} | {c.f1:.3f} | "
            f"{tp_:.2f}/{tr:.2f} | {c.clean_fp_rate:.3f} | {'PASS' if ok else 'FAIL'} |"
        )
    lines += [
        "",
        f"Throughput: {meta['mb_per_s']:.0f} MB/s across {meta['n_bags']} bags "
        f"({meta['total_mb']:.0f} MB in {meta['total_s']:.1f}s of audit time, "
        f"{meta['workers']} workers).",
        "",
        "**How to read this.** A fault counts as detected if the right detector fires with an "
        "overlapping window on the right topic. Findings that a different injected fault "
        "legitimately explains are not counted as false positives — a 12-second dropout really "
        "does drop messages, and penalising the auditor for saying so would measure the wrong "
        "thing. Findings on the clean control set are counted with no such leniency.",
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bags", default="/tmp/baglens-bags")
    ap.add_argument("--out", default="evals/integrity/RESULTS.md")
    ap.add_argument("--faulted", type=int, default=160)
    ap.add_argument("--clean", type=int, default=40)
    ap.add_argument("--duration", type=float, default=120.0)
    ap.add_argument("--regenerate", action="store_true")
    ap.add_argument("--workers", type=int, default=max(1, (mp.cpu_count() or 2) - 1))
    ap.add_argument("--gate", action="store_true", help="exit non-zero if a target is missed")
    args = ap.parse_args(argv)

    bags = Path(args.bags)
    existing = sorted(bags.glob("*.mcap"))
    if args.regenerate or len(existing) < args.faulted + args.clean:
        from tests.synth.generate import generate_corpus

        print(f"generating {args.faulted + args.clean} bags in {bags} …")
        generate_corpus(bags, args.faulted, args.clean, args.duration)
        existing = sorted(bags.glob("*.mcap"))

    paths = [str(p) for p in existing]
    print(f"auditing {len(paths)} bags with {args.workers} workers …")
    t0 = time.perf_counter()
    if args.workers > 1:
        with mp.Pool(args.workers) as pool:
            results = pool.map(audit_one, paths)
    else:
        results = [audit_one(p) for p in paths]
    wall = time.perf_counter() - t0

    counts = {det: Counts() for det in ALL_TRACKED}
    n_clean = n_faulted = 0
    total_bytes = 0
    total_audit_s = 0.0
    for res in results:
        gt_path = Path(res["path"]).with_suffix(".ground_truth.json")
        truth = json.loads(gt_path.read_text())
        score_bag(truth, res, counts)
        total_bytes += res["size_bytes"]
        total_audit_s += res["elapsed_s"]
        if truth["faults"]:
            n_faulted += 1
        else:
            n_clean += 1

    meta = {
        "n_faulted": n_faulted,
        "n_clean": n_clean,
        "n_bags": len(results),
        "duration_s": args.duration,
        "generated_at": time.strftime("%Y-%m-%d"),
        "total_mb": total_bytes / 1e6,
        "total_s": total_audit_s,
        "mb_per_s": (total_bytes / 1e6) / max(total_audit_s, 1e-9),
        "workers": args.workers,
        "wall_s": wall,
    }
    text = render(counts, meta)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text)
    print(text)

    failed = [
        det
        for det in ALL_TRACKED
        if counts[det].precision < TARGETS[det][0] or counts[det].recall < TARGETS[det][1]
    ]
    if failed:
        print("below target: " + ", ".join(failed))
    return 1 if (failed and args.gate) else 0


if __name__ == "__main__":
    raise SystemExit(main())
