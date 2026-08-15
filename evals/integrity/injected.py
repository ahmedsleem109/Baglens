"""Precision and recall for all eight detectors on **real recordings with known faults**.

The two numbers this repository already publishes each answer half the question:

* `RESULTS.md` — 1.000/1.000 across eight fault classes, on a background this repository
  generated. It proves the detectors and the generator agree.
* `REAL_DATA.md` — 0.993/0.942 against labels PX4's logger wrote itself. Real background,
  real labels, but only one detector and only one fault class.

This eval is the missing cell: **real background, eight detectors, exact labels.**
`tests/synth/inject.py` copies a real `ros2 bag record` MCAP and removes, thins, stretches
or shifts a known window of it. The jitter, burstiness, topic mix and QoS are the robot's;
the fault is ours, and we know where we put it.

**Scoring is differential, and that is the whole design.** A real recording has findings of
its own — `nuway_waypoints` drops 4.5% of `/odometry/global` with nobody's help. Counting
those against the detector would measure the recording's health, not the detector's
accuracy. So every base is also copied *clean*, audited, and its findings become a
baseline: a finding that appears in the clean control is a property of the recording, and
only findings that appear when the fault does are attributed to it.

**What this does not prove.** That injected faults are caught against a real background is
not the same claim as that every naturally-occurring fault is caught. Injection cannot
produce a fault shape nobody thought of. It is one rung above a synthetic corpus and one
rung below an instrumented robot, and the number should be read that way.

    uv run python -m evals.integrity.injected --bags /tmp/baglens-injected \
        --out evals/integrity/INJECTED.md
"""

from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .run import (
    ALIASES,
    ALL_TRACKED,
    EXPECTED,
    EXPLAINS,
    _fault_topics,
    _overlaps,
    _window,
    audit_one,
)

#: slack when deciding whether a faulted copy's finding is "the same finding" the clean
#: control already had. Wider than the matching tolerance because a clock step moves every
#: subsequent finding by up to its own size.
BASELINE_TOL_S = 4.0

#: Consequences of *how* faults are injected into a real file, which the synthetic
#: generator does not produce and `run.EXPLAINS` therefore does not list.
#:
#: `inject` moves `log_time` and leaves `publish_time` alone, because that is what a
#: recorder-side fault does — the publisher stamped the message when it stamped it. The
#: side effect is that a jitter kick or a clock step really does reorder log times against
#: publish times, and the clock detector really should say so. Counting that as a false
#: positive would penalise the detector for being right about the file in front of it.
EXTRA_EXPLAINS: dict[str, tuple[str, ...]] = {
    "jitter_injection": ("clock_step", "clock"),
    "clock_step": ("clock_lag",),
    "recorder_lag": ("clock_step", "clock"),
}


@dataclass
class Counts:
    tp: int = 0
    fp: int = 0
    fn: int = 0
    labels: int = 0
    baseline_findings: int = 0

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


@dataclass
class BaseResult:
    stem: str
    duration_s: float
    n_topics: int
    baseline: list[dict[str, Any]] = field(default_factory=list)
    rows: list[tuple[str, str, bool, int]] = field(default_factory=list)


def _norm(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {**f, "detector": ALIASES.get(f["detector"], f["detector"])}
        for f in findings
        if ALIASES.get(f["detector"], f["detector"]) in ALL_TRACKED
    ]


def is_baseline(f: dict[str, Any], baseline: list[dict[str, Any]]) -> bool:
    """Did the untouched copy of this same recording already say this?

    Matched on detector, topic and an overlapping window — not on the summary string,
    which carries counts that move when messages are removed.
    """
    for b in baseline:
        if b["detector"] != f["detector"] or b["topic"] != f["topic"]:
            continue
        if _overlaps((b["t_start"], b["t_end"]), (f["t_start"], f["t_end"]), BASELINE_TOL_S):
            return True
    return False


def score_variant(truth: dict[str, Any], result: dict[str, Any],
                  baseline: list[dict[str, Any]], counts: dict[str, Counts]
                  ) -> tuple[int, int, list[str]]:
    """Score one faulted copy against its labels. Returns (hits, misses, fp summaries)."""
    duration = float(truth["duration_s"])
    # A fault that removed or moved no messages is not a fault: it was planned against a
    # topic that turned out not to publish in the copied window. Scoring it would hand
    # every detector a free miss for something that was never injected. `inject` records
    # the count; older ground truth predates the field and is taken at its word.
    faults = [f for f in truth["faults"] if f.get("effective", True)]
    findings = [f for f in _norm(result["findings"]) if not is_baseline(f, baseline)]

    matched: set[int] = set()
    hits = misses = 0
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
            matched.add(i)
        primary = want[0] if want else None
        if primary:
            counts[primary].labels += 1
            if hit:
                counts[primary].tp += 1
                hits += 1
            else:
                counts[primary].fn += 1
                misses += 1

    explained: set[str] = set()
    for fault in faults:
        explained |= set(EXPLAINS.get(fault["kind"], ()))
        explained |= set(EXTRA_EXPLAINS.get(fault["kind"], ()))
    fps: list[str] = []
    for i, f in enumerate(findings):
        if i in matched or f["detector"] in explained:
            continue
        counts[f["detector"]].fp += 1
        fps.append(f"{f['detector']} on {f['topic'] or '-'}: {f['summary'][:70]}")
    return hits, misses, fps


def render(counts: dict[str, Counts], bases: list[BaseResult], meta: dict[str, Any]) -> str:
    n_labels = sum(c.labels for c in counts.values())
    tp = sum(c.tp for c in counts.values())
    fp = sum(c.fp for c in counts.values())
    fn = sum(c.fn for c in counts.values())
    prec = tp / (tp + fp) if (tp + fp) else 1.0
    rec = tp / (tp + fn) if (tp + fn) else 1.0

    lines = [
        "# Detector performance on real recordings with injected faults",
        "",
        f"Corpus: **{meta['n_bases']} real recordings** from `~/data/public/ros2` "
        f"({meta['platforms']}), each copied clean and once per fault class — "
        f"**{meta['n_copies']} copies, {n_labels} exact labels**, "
        f"{meta['total_gb']:.1f} GB. Generated {meta['generated_at']}.",
        "",
        "**The background is real; the fault is ours.** Every copy keeps the source's own "
        "jitter, burstiness, topic mix and QoS metadata. `tests/synth/inject.py` then "
        "removes, thins, stretches or shifts one known window. That is the cell neither "
        "`RESULTS.md` (synthetic background) nor `REAL_DATA.md` (one detector, one fault "
        "class) fills.",
        "",
        "**Scoring is differential.** Each base is also audited clean, and its findings "
        "become a baseline — a real recording has findings of its own, and counting those "
        "against the detector would measure the recording, not the detector. Only findings "
        "that appear *with* the fault are attributed to it.",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Injected labels | {n_labels} |",
        f"| **Recall** | **{rec:.3f}** |",
        f"| **Precision** | **{prec:.3f}** |",
        f"| Attributed false positives | {fp} |",
        f"| Baseline findings excluded | {meta['baseline_findings']} |",
        "",
        "## Per detector",
        "",
        "| Detector | Labels | TP | FP | FN | Precision | Recall | F1 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for det in ALL_TRACKED:
        c = counts[det]
        if not c.labels and not c.fp:
            continue
        lines.append(
            f"| `{det}` | {c.labels} | {c.tp} | {c.fp} | {c.fn} | "
            f"{c.precision:.3f} | {c.recall:.3f} | {c.f1:.3f} |"
        )

    lines += [
        "",
        "## Per recording",
        "",
        "| Recording | Duration | Topics | Baseline findings | Variant | Label caught |",
        "|---|---|---|---|---|---|",
    ]
    for b in bases:
        first = True
        for variant, kind, hit, n_fp in b.rows:
            head = (f"| `{b.stem}` | {b.duration_s:.0f}s | {b.n_topics} | "
                    f"{len(b.baseline)} |") if first else "| | | | |"
            first = False
            mark = "yes" if hit else "**no**"
            extra = f" (+{n_fp} FP)" if n_fp else ""
            lines.append(f"{head} `{variant}` ({kind}) | {mark}{extra} |")

    lines += [
        "",
        "## The first run, and what changed",
        "",
        "The first version of this corpus scored **0.615 recall / 0.800 precision over 39 "
        "labels**, and the number is recorded here because the reason it moved is the kind "
        "of thing that usually disappears from a README.",
        "",
        "Nothing about the detectors changed — no threshold, no rule, no line of "
        "`src/baglens`. What changed is that the corpus had been writing labels the "
        "detectors do not claim to be able to satisfy:",
        "",
        "* Four of five `rate_degradation` labels sat on recordings shorter than D3's "
        "minimum history (`min_buckets * bucket_s` = 80 s), and the fifth spread its ramp "
        "across 1843 s when D3 can only see 300 s of slope at a time.",
        "* Three of five `recorder_lag` labels grew less total lag than D6's 100 ms floor, "
        "because the growth was specified per minute and applied to a 16-second slice.",
        "* Two `jitter` labels targeted topics with fewer messages than D4's variance "
        "window, so no baseline could exist to expand.",
        "* Six findings counted as false positives were consequences of the injection "
        "method rather than of the detectors: moving `log_time` and leaving `publish_time` "
        "alone genuinely reorders the two, and the clock detector was correct to say so.",
        "",
        "A label a detector could not satisfy measures the length of the recording, not "
        "the accuracy of the detector. Fault magnitudes are now matched to the synthetic "
        "corpus so the two numbers compare, and faults whose floor a recording cannot "
        "clear are not written — which is why the label count fell from 39 to 34.",
        "",
        "## What this measures, and what it does not",
        "",
        "It measures whether a fault of a known shape, placed at a known time in a real "
        "recording, is found by the detector that claims that shape. It does **not** "
        "measure whether every naturally-occurring fault is found: injection can only "
        "produce fault shapes someone thought of, and a recording's own defects are "
        "excluded by the baseline rather than scored.",
        "",
        "Two labels are weaker than the rest and are marked as such wherever they matter. "
        "`diffuse_drops` and `recorder_lag` are whole-run faults, so any finding of the "
        "right kind anywhere in the recording matches them — a coarser test than the "
        "windowed faults get. And a fault injected into a topic the recording barely "
        "publishes is not detectable in principle; `inject.plan_for` only targets topics "
        "above 1 Hz for that reason.",
        "",
        f"Reproduce: `uv run python -m tests.synth.inject --sources {meta['sources']} "
        f"--out {meta['bags']}` then `uv run python -m evals.integrity.injected "
        f"--bags {meta['bags']}`.",
        "",
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bags", default="/tmp/baglens-injected")
    ap.add_argument("--sources", default="~/data/public/ros2")
    ap.add_argument("--out", default="evals/integrity/INJECTED.md")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--build", action="store_true", help="build the corpus first if absent")
    ap.add_argument("--max-gb", type=float, default=6.0)
    ap.add_argument("--gate", action="store_true")
    ap.add_argument("--min-recall", type=float, default=0.80)
    args = ap.parse_args(argv)

    bags = Path(args.bags).expanduser()
    if args.build or not bags.exists() or not list(bags.glob("*.mcap")):
        from tests.synth.inject import build_corpus

        build_corpus(Path(args.sources).expanduser(), bags, max_gb=args.max_gb)

    paths = sorted(str(p) for p in bags.glob("*.mcap"))
    if not paths:
        print(f"no copies in {bags}")
        return 1
    print(f"auditing {len(paths)} copies with {args.workers} workers …")
    t0 = time.perf_counter()
    if args.workers > 1:
        with mp.Pool(args.workers) as pool:
            results = pool.map(audit_one, paths)
    else:
        results = [audit_one(p) for p in paths]
    print(f"audited in {time.perf_counter() - t0:.0f}s")

    by_path = {r["path"]: r for r in results}
    groups: dict[str, list[str]] = defaultdict(list)
    for p in paths:
        groups[Path(p).stem.split("__")[0]].append(p)

    counts = {det: Counts() for det in ALL_TRACKED}
    bases: list[BaseResult] = []
    total_bytes = 0
    n_baseline = 0

    for stem, members in sorted(groups.items()):
        clean = next((m for m in members if m.endswith("__clean.mcap")), None)
        if clean is None:
            print(f"skip {stem}: no clean control")
            continue
        baseline = _norm(by_path[clean]["findings"])
        n_baseline += len(baseline)
        truth_clean = json.loads(Path(clean).with_suffix(".ground_truth.json").read_text())
        br = BaseResult(stem=stem, duration_s=float(truth_clean["duration_s"]),
                        n_topics=len(truth_clean["topics"]), baseline=baseline)

        for m in members:
            total_bytes += Path(m).stat().st_size
            if m == clean:
                continue
            truth = json.loads(Path(m).with_suffix(".ground_truth.json").read_text())
            hits, misses, fps = score_variant(truth, by_path[m], baseline, counts)
            variant = Path(m).stem.split("__")[1]
            kind = truth["faults"][0]["kind"] if truth["faults"] else "clean"
            br.rows.append((variant, kind, misses == 0 and hits > 0, len(fps)))
            flag = "" if (misses == 0 and hits > 0) else "  MISS"
            print(f"  {Path(m).name:44s} {kind:18s} hit={hits} miss={misses} "
                  f"fp={len(fps)}{flag}")
            for line in fps:
                print(f"        FP  {line}")
        bases.append(br)

    for det in ALL_TRACKED:
        counts[det].baseline_findings = sum(
            1 for b in bases for f in b.baseline if f["detector"] == det
        )

    meta = {
        "n_bases": len(bases),
        "n_copies": len(paths),
        "platforms": "shuttle bus, teleop arm rig, Tesla Model 3 CAN, handheld LIVO",
        "total_gb": total_bytes / 1e9,
        "generated_at": time.strftime("%Y-%m-%d"),
        "baseline_findings": n_baseline,
        "sources": args.sources,
        "bags": args.bags,
    }
    text = render(counts, bases, meta)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text)
    print("\n" + text)

    tp = sum(c.tp for c in counts.values())
    fn = sum(c.fn for c in counts.values())
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    if args.gate and recall < args.min_recall:
        print(f"FAIL: recall {recall:.3f} < {args.min_recall}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
