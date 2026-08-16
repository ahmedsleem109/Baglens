"""Precision and recall for F1 — end-to-end data age.

Two halves, because they answer different questions and the second is the one that counts.

**Synthetic.** Pipeline fixtures where the propagation graph and every stage delay are
known exactly, so the inferred graph can be checked against ground truth and not merely
against plausibility. A stale-pipeline fault grows one stage's delay over the run.

**Real background.** A real `ros2 bag record` MCAP, with the stamps of one topic moved
earlier by a growing amount. Arrival times, message sizes, topic mix and QoS are the
robot's; the only thing changed is eight bytes per message of one topic. Every base is
also copied clean and audited, and findings present in the clean control are subtracted —
a real recording has stamp problems of its own, and counting those against the detector
would measure the recording rather than the detector.

**What this cannot prove.** Injection produces the fault shape we thought of. A ramp is
what a queue backing up looks like; it is not what every stale-data fault looks like.

    uv run python -m evals.age.data_age --out evals/age/DATA_AGE.md
    uv run python -m evals.age.data_age --skip-real     # synthetic only, ~2 min
"""

from __future__ import annotations

import argparse
import json
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from baglens.detectors.auditor import Auditor
from baglens.readers.base import open_bag
from tests.synth.generate import (
    DEFAULT_PIPELINE,
    RESTAMPING_PIPELINE,
    UNSTAMPED_PIPELINE,
    generate_bag,
    stale_pipeline,
)
from tests.synth.inject import inject

#: the trend needs min_buckets * bucket_s of history before it may claim anything, so a
#: base shorter than this cannot support the fault at all. Named here rather than
#: silently excluded.
MIN_BASE_S = 120.0

#: Synthetic: the stage's delay is ramped *to* this multiple of its healthy value, so 1.0
#: would be no fault at all and is not a case.
SYNTH_FACTORS = (2.0, 4.0, 8.0)

#: Real: the age *added* over the window, in units of the topic's own P50→P95 spread.
#: 1.0 injects a fault exactly the size of the noise it hides in — included deliberately
#: as the floor of what is detectable in principle, not as a case that ought to pass.
RAMP_FACTORS = (1.0, 2.0, 4.0, 8.0)

SEEDS = (11, 22, 33, 44, 55)


@dataclass
class Counts:
    tp: int = 0
    fp: int = 0
    fn: int = 0

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if (self.tp + self.fp) else 1.0

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if (self.tp + self.fn) else 1.0


@dataclass
class Case:
    name: str
    detected: bool
    expected_topic: str
    fired_on: list[str] = field(default_factory=list)
    detail: str = ""


def _age_trend_topics(report: Any) -> list[str]:
    """Topics with a growing-age finding. The trend rule is the one under test."""
    return [
        f.topic for f in report.findings
        if f.detector == "data_age" and f.topic and "age is growing" in f.summary
    ]


def _audit(path: Path) -> Any:
    return Auditor(open_bag(path)).run()


# --------------------------------------------------------------------- synthetic


def synthetic(tmp: Path) -> tuple[Counts, list[Case], dict[str, Any]]:
    """Clean pipelines must stay silent; ramped ones must be caught, on the right topic."""
    counts = Counts()
    cases: list[Case] = []

    for seed in SEEDS:
        p = tmp / f"clean_{seed}.mcap"
        generate_bag(p, seed=seed, duration_s=240.0, topics=(),
                     pipelines=(DEFAULT_PIPELINE,))
        fired = _age_trend_topics(_audit(p))
        counts.fp += len(fired)
        cases.append(Case(f"clean seed={seed}", not fired, "", fired))

    stage = DEFAULT_PIPELINE.stages[1]  # /detections, the perception stage
    healthy_ms = stage.delay_s * 1000.0
    for seed in SEEDS:
        for factor in SYNTH_FACTORS:
            to_ms = healthy_ms * factor
            p = tmp / f"ramp_{seed}_{factor:g}.mcap"
            generate_bag(
                p, seed=seed, duration_s=240.0, topics=(),
                pipelines=(DEFAULT_PIPELINE,),
                faults=[stale_pipeline(stage.topic, healthy_ms, to_ms, 60.0, 120.0)],
            )
            fired = _age_trend_topics(_audit(p))
            hit = stage.topic in fired
            counts.tp += int(hit)
            counts.fn += int(not hit)
            cases.append(Case(
                f"ramp x{factor:g} seed={seed}", hit, stage.topic, fired,
                f"{healthy_ms:.0f} -> {to_ms:.0f} ms",
            ))

    # the two rules that are not the trend: an unmeasurable stage must be named, and a
    # restamping node must be caught
    structural: dict[str, Any] = {}
    p = tmp / "unstamped.mcap"
    generate_bag(p, seed=7, duration_s=120.0, topics=(), pipelines=(UNSTAMPED_PIPELINE,))
    rep = _audit(p)
    structural["unmeasurable_named"] = "/cmd_vel" in (rep.data_age.unmeasurable or {})
    structural["unmeasurable_not_faked"] = all(
        s.topic != "/cmd_vel" for s in rep.data_age.stages
    )

    p = tmp / "restamp.mcap"
    generate_bag(p, seed=8, duration_s=120.0, topics=(), pipelines=(RESTAMPING_PIPELINE,))
    rep = _audit(p)
    structural["restamp_caught"] = any(
        f.detector == "data_age" and f.topic == "/detections"
        and "own publish time" in f.summary
        for f in rep.findings
    )

    # the inferred graph must match the pipeline that was actually written
    p = tmp / "graph.mcap"
    generate_bag(p, seed=9, duration_s=120.0, topics=(), pipelines=(DEFAULT_PIPELINE,))
    rep = _audit(p)
    edges = {s.topic: s.upstream for s in rep.data_age.stages}
    want = {
        DEFAULT_PIPELINE.stages[0].topic: None,
        DEFAULT_PIPELINE.stages[1].topic: DEFAULT_PIPELINE.stages[0].topic,
        DEFAULT_PIPELINE.stages[2].topic: DEFAULT_PIPELINE.stages[1].topic,
    }
    structural["graph_correct"] = edges == want
    structural["graph_inferred"] = edges
    structural["stage_delays_ms"] = {
        s.topic: s.stage_p50_ms for s in rep.data_age.stages
    }
    return counts, cases, structural


# -------------------------------------------------------------------------- real


def real(bases: list[Path], tmp: Path) -> tuple[Counts, list[Case], list[dict[str, Any]]]:
    """Same fault, real background, differential scoring against a clean control."""
    counts = Counts()
    cases: list[Case] = []
    profiles: list[dict[str, Any]] = []

    for base in bases:
        meta = open_bag(base).metadata()
        if meta.duration_s < MIN_BASE_S:
            cases.append(Case(f"{base.name} (skipped)", False, "",
                              detail=f"{meta.duration_s:.0f}s < {MIN_BASE_S:.0f}s minimum"))
            continue

        window = min(meta.duration_s, 600.0)
        control = tmp / f"{base.stem}_control.mcap"
        inject(base, control, [], window_s=window)
        rep = _audit(control)
        baseline = set(_age_trend_topics(rep))
        counts.fp += len(baseline)

        # Pick targets by *stability*, not by popularity. The first version of this eval
        # ranked by message count and chose `/diagnostics` on nuway_stops — P50 1.4 ms but
        # a natural P99 of 26 ms — then injected a 5 ms ramp into it. A fault smaller than
        # the topic's own noise measures nothing about the detector, and it produced a
        # recall of 0.222 that said more about the experiment than about the code.
        candidates = [
            s for s in rep.data_age.stages
            if s.messages >= 2000 and s.age_p95_ms > 0.0
        ]
        stages = sorted(candidates, key=lambda s: (s.age_p95_ms - s.age_p50_ms))
        profiles.append({
            "base": base.name,
            "duration_s": round(window, 1),
            "unmeasurable": rep.data_age.unmeasurable,
            "stages": [
                {"topic": s.topic, "p50": s.age_p50_ms, "p95": s.age_p95_ms,
                 "p99": s.age_p99_ms, "messages": s.messages}
                for s in rep.data_age.stages
            ],
            "baseline_trend_findings": sorted(baseline),
        })
        if not stages:
            cases.append(Case(f"{base.name} (no target)", False, "",
                              detail="no headered topic with a measurable age"))
            continue

        pick = stages[0]
        target = pick.topic
        # The injected delta is expressed in units of the topic's own noise band, so the
        # difficulty of each case is stated rather than accidental. `noise` is the P50→P95
        # spread the topic already has; a delta below it is genuinely undetectable and a
        # detector that fired on it would be reporting the recording's own variance.
        noise = max(pick.age_p95_ms - pick.age_p50_ms, 1.0)
        for factor in RAMP_FACTORS:
            delta = noise * factor
            out = tmp / f"{base.stem}_x{factor:g}.mcap"
            # from 0: the ramp adds age over the window rather than offsetting the whole
            # recording. An offset applied from t=0 lifts the baseline the trend is
            # measured against, which hides the very growth being injected.
            inject(
                base, out,
                [stale_pipeline(target, 0.0, delta, window * 0.25, window * 0.5)],
                window_s=window,
            )
            fired = set(_age_trend_topics(_audit(out))) - baseline
            hit = target in fired
            counts.tp += int(hit)
            counts.fn += int(not hit)
            cases.append(Case(
                f"{base.name} x{factor:g}", hit, target, sorted(fired),
                f"+{delta:.0f} ms on `{target}` "
                f"(P50 {pick.age_p50_ms:.1f}, noise {noise:.1f} ms, SNR {factor:g})",
            ))
    return counts, cases, profiles


# ------------------------------------------------------------------------ render


def render(syn: Counts, syn_cases: list[Case], structural: dict[str, Any],
           rl: Counts | None, rl_cases: list[Case], profiles: list[dict[str, Any]],
           elapsed: float) -> str:
    lines = [
        "# F1 — end-to-end data age: precision and recall",
        "",
        "Regenerate with `uv run python -m evals.age.data_age`. Every number below comes "
        "from that command; none is transcribed by hand.",
        "",
        f"Generated in {elapsed:.0f}s.",
        "",
        "## The claim the whole feature rests on",
        "",
        "`header.stamp` is read as an 8-byte peek at CDR offset 4 rather than by "
        "deserializing. That is verified separately, against a full decode, by "
        "`scripts/verify_stamp_peek.py` — see `docs/how-it-works.md`. If that check ever "
        "fails on a corpus, none of the numbers here mean anything on it.",
        "",
        "## Synthetic pipelines",
        "",
        f"| | |\n|---|---|\n| precision | {syn.precision:.3f} |\n"
        f"| recall | {syn.recall:.3f} |\n"
        f"| ramps injected | {syn.tp + syn.fn} |\n"
        f"| clean pipelines | {len(SEEDS)} |",
        "",
        "Ramp magnitudes are multiples of the healthy stage delay: "
        + ", ".join(f"x{f:g}" for f in SYNTH_FACTORS)
        + f". Seeds: {', '.join(str(s) for s in SEEDS)}.",
        "",
        "### Rules that are not the trend",
        "",
        f"* unmeasurable stage named, not invented: "
        f"**{structural['unmeasurable_named'] and structural['unmeasurable_not_faked']}**",
        f"* restamping node caught: **{structural['restamp_caught']}**",
        f"* propagation graph inferred correctly: **{structural['graph_correct']}**",
        "",
        "The graph is inferred from stamp equality alone — nothing declares it:",
        "",
        "```",
        json.dumps(structural["graph_inferred"], indent=2),
        "```",
        "",
        "Per-stage delay recovered (ms, P50):",
        "",
        "```",
        json.dumps(structural["stage_delays_ms"], indent=2),
        "```",
        "",
    ]

    if rl is not None:
        lines += [
            "## Real background, injected fault",
            "",
            f"| | |\n|---|---|\n| precision | {rl.precision:.3f} |\n"
            f"| recall | {rl.recall:.3f} |\n"
            f"| ramps injected | {rl.tp + rl.fn} |",
            "",
            "Scored differentially: each base is also copied clean, and any trend finding "
            "present in that control is subtracted before the faulted copy is scored.",
            "",
            "### Data age measured on the unmodified recordings",
            "",
            "This is the number F1 exists to produce, and it is reported here on real "
            "robots rather than on fixtures.",
            "",
        ]
        for prof in profiles:
            lines += [
                f"#### {prof['base']} ({prof['duration_s']:.0f}s)",
                "",
                "| topic | P50 ms | P95 ms | P99 ms | messages |",
                "|---|---:|---:|---:|---:|",
            ]
            for s in prof["stages"]:
                lines.append(
                    f"| `{s['topic']}` | {s['p50']:.1f} | {s['p95']:.1f} | "
                    f"{s['p99']:.1f} | {s['messages']:,} |"
                )
            if prof["unmeasurable"]:
                lines += ["", "Unmeasurable — named, never given an age from arrival time:", ""]
                for topic, why in sorted(prof["unmeasurable"].items()):
                    lines.append(f"* `{topic}` — {why}")
            lines.append("")

        lines += ["### Cases", "", "| case | expected | detected | fired on |",
                  "|---|---|---|---|"]
        for c in rl_cases:
            lines.append(
                f"| {c.name} | `{c.expected_topic or '—'}` | "
                f"{'yes' if c.detected else 'no'} | "
                f"{', '.join(f'`{t}`' for t in c.fired_on) or '—'} |"
            )
        lines.append("")

    lines += [
        "## Synthetic cases",
        "",
        "| case | detail | detected | fired on |",
        "|---|---|---|---|",
    ]
    for c in syn_cases:
        lines.append(
            f"| {c.name} | {c.detail or '—'} | {'yes' if c.detected else 'no'} | "
            f"{', '.join(f'`{t}`' for t in c.fired_on) or '—'} |"
        )
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=Path("evals/age/DATA_AGE.md"))
    ap.add_argument("--corpus", type=Path, default=Path.home() / "data/public/ros2")
    ap.add_argument("--skip-real", action="store_true")
    ap.add_argument("--keep", type=Path, default=None,
                    help="keep generated bags here instead of a temp dir")
    args = ap.parse_args(argv)

    t0 = time.perf_counter()
    tmp = args.keep or Path(tempfile.mkdtemp(prefix="baglens-age-"))
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        syn, syn_cases, structural = synthetic(tmp)
        print(f"synthetic: precision {syn.precision:.3f} recall {syn.recall:.3f}")

        rl: Counts | None = None
        rl_cases: list[Case] = []
        profiles: list[dict[str, Any]] = []
        if not args.skip_real:
            bases = sorted(args.corpus.glob("*.mcap"))
            rl, rl_cases, profiles = real(bases, tmp)
            print(f"real:      precision {rl.precision:.3f} recall {rl.recall:.3f}")

        text = render(syn, syn_cases, structural, rl, rl_cases, profiles,
                      time.perf_counter() - t0)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text)
        print(f"wrote {args.out}")
    finally:
        if args.keep is None:
            shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
