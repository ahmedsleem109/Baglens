"""F2 — the pre-flight readiness gate. Refuse to start a mission that is already broken.

A field test day costs thousands and gets burned because a node did not launch, a sensor
came up in the wrong mode, the clock was not synced, or a topic was already degrading
before anyone drove anywhere. It is discovered that evening, in the bag.

    baglens preflight --record --from known_good.mcap --out fleet_baseline.json
    baglens preflight --expect fleet_baseline.json --for 30s

Almost all of this is assembly. `live.py`, `ros2.py` and the cadence detectors already
exist and are measured; this file adds a comparison against a captured baseline and a
verdict with an exit code. No new detector, no new threshold that is not stated here.

Three things it has to get right, none of them about detection:

* **It must be fast to say yes.** A gate that takes five minutes gets skipped, and a
  skipped gate is worse than no gate because it manufactures confidence.
* **It must never call an unchecked thing a passing thing.** Thirty seconds is shorter
  than cadence warmup on a slow topic, so some topics genuinely cannot be judged. Those
  are reported `unchecked` and listed in the verdict. This is `unassessable` again, in the
  one place where the cost of getting it wrong is a lost field day.
* **It must be usable from a launch file, a CI job, and a human at a laptop in a field.**
  Hence: exit code, JSON, and one screen of text, from the same run.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .config import CONFIG, Config
from .live import LiveMonitor
from .models import (
    Baseline,
    BaselineTopic,
    HealthReport,
    PreflightCheck,
    PreflightReport,
    Severity,
)
from .provenance import Provenance

#: detectors the gate runs. `correlation` and `file_integrity` are deliberately absent:
#: the first needs more history than thirty seconds to mean anything, and the second is
#: about a file, which a live graph does not have.
GATE_DETECTORS = ["cadence", "gap", "rate_degradation", "jitter", "clock", "data_age"]

#: findings that mean "this is already going wrong", checked live. D3 and D4 are the
#: leading indicators — they fire while a topic is still publishing, which is the only
#: kind of warning a gate can act on.
DEGRADING_DETECTORS = {"rate_degradation", "jitter", "dropped"}


# --------------------------------------------------------------------------- record


def baseline_from(report: HealthReport, source: str = "") -> Baseline:
    """Capture what "normal" looks like from a run that was known good.

    Deliberately derived rather than declared: the rates that matter are the ones this
    robot actually achieves, not the ones on the sensor's datasheet. A topic the audit
    could not measure is omitted, so the gate will report it unchecked rather than
    comparing against a number that was a guess when it was written.
    """
    ages = {s.topic: s for s in (report.data_age.stages if report.data_age else [])}
    topics: dict[str, BaselineTopic] = {}
    for th in report.topics:
        if th.observed_hz <= 0 or th.count <= 0:
            continue
        age = ages.get(th.topic)
        topics[th.topic] = BaselineTopic(
            msg_type=th.msg_type,
            hz=round(th.observed_hz, 4),
            jitter_cv=round(th.jitter_cv, 4),
            age_p95_ms=(age.age_p95_ms if age and age.age_p95_ms > 0 else None),
        )
    return Baseline(
        source=source or report.path,
        captured_duration_s=round(report.duration_s, 3),
        topics=topics,
    )


# ------------------------------------------------------------------------ evaluate


def evaluate(
    baseline: Baseline, report: HealthReport, elapsed_s: float,
    cfg: Config | None = None, baseline_source: str = "",
) -> PreflightReport:
    """Compare what the graph is doing now against what it did when it was good."""
    cfg = cfg or CONFIG
    p = cfg.preflight
    checks: list[PreflightCheck] = []
    # How much *data time* was observed, which is not the wall clock: a replayed
    # recording covers 30 s of graph in a second, and expected-message counts have to be
    # against the span the data covers or every check silently loosens.
    span_s = report.duration_s or elapsed_s
    seen = {th.topic: th for th in report.topics}
    ages = {s.topic: s for s in (report.data_age.stages if report.data_age else [])}
    unmeasurable = (report.data_age.unmeasurable if report.data_age else {}) or {}

    for topic, want in sorted(baseline.topics.items()):
        th = seen.get(topic)

        # -- is it there at all? The one check that is never "unchecked": a topic that
        # published nothing in the window has failed, however slow it was expected to be.
        if th is None or th.count == 0:
            checks.append(PreflightCheck(
                check="topic_present", topic=topic, status="fail",
                detail=f"expected {want.hz:.1f} Hz, nothing published in "
                       f"{elapsed_s:.0f}s",
                expected=want.hz, observed=0.0,
            ))
            continue
        checks.append(PreflightCheck(
            check="topic_present", topic=topic, status="pass",
            detail=f"{th.count} messages", observed=float(th.count),
        ))

        # -- coverage. A topic can hold its nominal rate and still be unfit: `observed_hz`
        # is the *modal* rate, robust to gaps by design, so a topic silent for two thirds
        # of the window still reports 10 Hz. Counting the messages that actually arrived
        # is what catches a node that dropped out and came back.
        expected_n = want.hz * span_s
        if expected_n >= p.min_messages:
            coverage = th.count / expected_n
            if coverage < 1.0 - p.rate_tolerance:
                checks.append(PreflightCheck(
                    check="coverage", topic=topic, status="fail",
                    detail=f"{th.count} of ~{expected_n:.0f} expected messages "
                           f"({coverage:.0%}); silent for "
                           f"{max(th.total_silent_s, 0.0):.1f}s of {span_s:.0f}s",
                    expected=round(expected_n, 1), observed=float(th.count),
                ))
            else:
                checks.append(PreflightCheck(
                    check="coverage", topic=topic, status="pass",
                    detail=f"{coverage:.0%} of expected messages",
                    expected=round(expected_n, 1), observed=float(th.count),
                ))

        # -- rate. Too few messages to judge is `unchecked`, not `pass`: at 30 s a 0.2 Hz
        # topic delivers six messages, and six messages do not establish a rate.
        if th.count < p.min_messages and expected_n < p.min_messages:
            checks.append(PreflightCheck(
                check="rate", topic=topic, status="unchecked",
                detail=f"{th.count} messages in {elapsed_s:.0f}s is too few to judge a "
                       f"{want.hz:.2f} Hz topic",
                expected=want.hz, observed=th.observed_hz,
            ))
        else:
            drift = (th.observed_hz - want.hz) / want.hz if want.hz else 0.0
            ok = abs(drift) <= p.rate_tolerance
            checks.append(PreflightCheck(
                check="rate", topic=topic, status="pass" if ok else "fail",
                detail=f"{th.observed_hz:.2f} Hz vs {want.hz:.2f} Hz baseline "
                       f"({drift:+.0%})",
                expected=want.hz, observed=round(th.observed_hz, 3),
            ))

        # -- data age (F1). Only where the baseline actually recorded one.
        age = ages.get(topic)
        if want.age_p95_ms is None:
            checks.append(PreflightCheck(
                check="data_age", topic=topic, status="unchecked",
                detail=unmeasurable.get(topic, "no age in the baseline to compare against"),
            ))
        elif age is None or age.age_p95_ms <= 0:
            checks.append(PreflightCheck(
                check="data_age", topic=topic, status="unchecked",
                detail=unmeasurable.get(topic, "no age measurable in this window"),
                expected=want.age_p95_ms,
            ))
        else:
            budget = max(want.age_p95_ms * p.age_tolerance, p.age_floor_ms)
            ok = age.age_p95_ms <= budget
            checks.append(PreflightCheck(
                check="data_age", topic=topic, status="pass" if ok else "fail",
                detail=f"P95 age {age.age_p95_ms:.0f} ms vs {want.age_p95_ms:.0f} ms "
                       f"baseline (budget {budget:.0f} ms)",
                expected=round(budget, 1), observed=round(age.age_p95_ms, 1),
            ))

    # -- topics publishing now that the baseline never saw. Not a failure: a graph may
    # legitimately carry more than the baseline did. Worth saying out loud all the same.
    for topic in sorted(set(seen) - set(baseline.topics)):
        if seen[topic].count:
            checks.append(PreflightCheck(
                check="topic_present", topic=topic, status="unchecked",
                detail="publishing, but absent from the baseline",
            ))

    # -- clocks. One check for the whole graph, because skew is not a per-topic property.
    clock = report.clock
    if clock is None:
        checks.append(PreflightCheck(check="clock", status="unchecked",
                                     detail="clock detector not run"))
    else:
        problems = []
        if clock.backward_jumps:
            problems.append(f"{clock.backward_jumps} backward jump(s)")
        if clock.steps:
            problems.append(f"{len(clock.steps)} clock step(s)")
        if abs(clock.lag_growth_s) >= cfg.clock.lag_growth_s:
            problems.append(f"recorder lag grew {clock.lag_growth_s * 1000:.0f} ms")
        checks.append(PreflightCheck(
            check="clock", status="fail" if problems else "pass",
            detail="; ".join(problems) or "consistent across publishers",
        ))

    # -- already degrading. The point of the gate: catch it before the mission, not after.
    degrading = [
        f for f in report.findings
        if f.detector in DEGRADING_DETECTORS and f.severity >= Severity.MEDIUM
    ]
    if degrading:
        for f in degrading:
            checks.append(PreflightCheck(
                check="degrading", topic=f.topic, status="fail", detail=f.summary,
            ))
    else:
        checks.append(PreflightCheck(
            check="degrading", status="pass",
            detail="no topic degrading during the window",
        ))

    # -- TF completeness is F3's, and it is not built. Claiming it here would be the
    # exact failure this gate exists to prevent, so it is declared unchecked by name.
    checks.append(PreflightCheck(
        check="tf", status="unchecked",
        detail="transform-tree completeness is not yet implemented (F3)",
    ))

    failures = [
        f"{c.topic + ': ' if c.topic else ''}{c.detail}"
        for c in checks if c.status == "fail"
    ]
    unchecked = [
        f"{c.check}{' ' + c.topic if c.topic else ''}: {c.detail}"
        for c in checks if c.status == "unchecked"
    ]
    no_go = bool(failures) or (p.strict and bool(unchecked))

    return PreflightReport(
        verdict="no_go" if no_go else "go",
        window_s=p.window_s,
        elapsed_s=round(elapsed_s, 2),
        messages=sum(th.count for th in report.topics),
        checks=checks,
        failures=failures,
        unchecked=unchecked,
        baseline_source=baseline_source or baseline.source,
        provenance=Provenance(
            method=f"preflight({','.join(GATE_DETECTORS)})",
            time_range=(0.0, round(elapsed_s, 3)),
        ),
    )


# ----------------------------------------------------------------------------- run


def watch(feed: Any, for_s: float, cfg: Config | None = None) -> tuple[HealthReport, float]:
    """Consume a feed for at most ``for_s`` seconds and return what was seen.

    The cap is wall clock and is enforced here rather than in the feed, so the same
    function bounds a live subscription and a replayed recording identically.
    """
    cfg = cfg or CONFIG
    monitor = LiveMonitor(feed, cfg, detectors=GATE_DETECTORS)
    started = time.monotonic()
    monitor.auditor._ensure_global_detectors()
    for arrival in feed.arrivals():
        monitor.auditor.push(arrival)
        monitor.n += 1
        # checked per arrival rather than on a timer: a graph that goes completely silent
        # is exactly the case the gate must not hang on, and it is handled by the feed's
        # own idle timeout, not by this loop spinning
        if time.monotonic() - started >= for_s:
            break
    elapsed = time.monotonic() - started
    return monitor.snapshot(), elapsed


def run(feed: Any, baseline: Baseline, for_s: float | None = None,
        cfg: Config | None = None, baseline_source: str = "") -> PreflightReport:
    cfg = cfg or CONFIG
    window = cfg.preflight.window_s if for_s is None else for_s
    report, elapsed = watch(feed, window, cfg)
    return evaluate(baseline, report, elapsed, cfg, baseline_source)


# -------------------------------------------------------------------------- render


def render_text(report: PreflightReport, width: int = 78) -> str:
    """One screen, readable on a laptop in a field, verdict on the first line."""
    mark = {"pass": "ok  ", "fail": "FAIL", "unchecked": "  ? "}
    head = "GO" if report.verdict == "go" else "NO-GO"
    lines = [
        f"{head} — {report.messages:,} messages in {report.elapsed_s:.1f}s",
        "",
    ]
    if report.failures:
        lines.append(f"{len(report.failures)} reason(s) not to fly:")
        lines += [f"  FAIL  {r}" for r in report.failures]
        lines.append("")

    by_check: dict[str, list[PreflightCheck]] = {}
    for c in report.checks:
        by_check.setdefault(c.check, []).append(c)
    for name, group in by_check.items():
        counts = {s: sum(1 for c in group if c.status == s) for s in
                  ("pass", "fail", "unchecked")}
        summary = ", ".join(f"{v} {k}" for k, v in counts.items() if v)
        lines.append(f"{name:<14} {summary}")
        for c in group:
            if c.status == "pass":
                continue
            topic = f"{c.topic} " if c.topic else ""
            lines.append(f"  {mark[c.status]} {topic}{c.detail}"[:width])

    if report.unchecked:
        lines += [
            "",
            f"{len(report.unchecked)} item(s) could not be checked in this window. "
            "They are not passes.",
        ]
    return "\n".join(lines)


# ----------------------------------------------------------------------------- cli


def _feed_for(source: str | None, speed: float) -> Any:
    """A live graph by default; a recording when one is named, for testing the gate."""
    if source:
        from .live import ReplayFeed

        return ReplayFeed(source, speed=speed)
    from .ros2 import Ros2Feed

    return Ros2Feed()


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json

    ap = argparse.ArgumentParser(
        prog="baglens preflight",
        description="Is this robot fit to record a mission? Green or red, with reasons.",
    )
    ap.add_argument("--expect", metavar="BASELINE.json",
                    help="baseline to compare against")
    ap.add_argument("--record", action="store_true",
                    help="capture a baseline from this run instead of judging it")
    ap.add_argument("--out", metavar="FILE", help="where --record writes the baseline")
    ap.add_argument("--for", dest="for_s", default="30s",
                    help="how long to watch (default 30s)")
    ap.add_argument("--from", dest="source", metavar="RECORDING",
                    help="replay a recording instead of subscribing to a live graph")
    ap.add_argument("--speed", type=float, default=0.0,
                    help="replay speed for --from; 0 means as fast as possible")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--strict", action="store_true",
                    help="treat unchecked items as failures")
    args = ap.parse_args(argv)

    for_s = float(args.for_s[:-1]) if str(args.for_s).endswith("s") else float(args.for_s)
    cfg = CONFIG.current

    if args.record:
        from .detectors.auditor import Auditor
        from .readers.base import open_bag

        if args.source:
            report = Auditor(open_bag(args.source), cfg, detectors=GATE_DETECTORS).run()
        else:
            report, _ = watch(_feed_for(None, args.speed), for_s, cfg)
        base = baseline_from(report, source=args.source or "ros2://graph")
        text = base.model_dump_json(indent=2)
        if args.out:
            Path(args.out).write_text(text)
            print(f"baseline: {len(base.topics)} topics from "
                  f"{base.captured_duration_s:.0f}s -> {args.out}")
        else:
            print(text)
        return 0

    if not args.expect:
        ap.error("--expect BASELINE.json is required (or use --record to make one)")
    baseline = Baseline.model_validate_json(Path(args.expect).read_text())

    if args.strict:
        from dataclasses import replace

        cfg = replace(cfg, preflight=replace(cfg.preflight, strict=True))

    report = run(_feed_for(args.source, args.speed), baseline, for_s, cfg,
                 baseline_source=args.expect)
    print(json.dumps(report.model_dump(mode="json"), indent=2) if args.json
          else render_text(report))
    return 0 if report.verdict == "go" else 1
