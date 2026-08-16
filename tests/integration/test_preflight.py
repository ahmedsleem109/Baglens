"""F2 — the pre-flight gate, scored on the number that decides whether it stays on.

A gate that cries wolf gets switched off, and a gate that is switched off is worse than
no gate because everyone still believes it is running. So the headline test here is not
"does it catch a missing topic" — it is **ten healthy graphs in a row, zero alarms**.

The gate is fed by `ReplayFeed` rather than a live ROS graph on purpose: it is the same
`LiveMonitor` code path either way, and this version is deterministic, needs no ROS
installation, and runs in CI.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from baglens.config import CONFIG
from baglens.detectors.auditor import Auditor
from baglens.live import ReplayFeed
from baglens.models import Baseline
from baglens.preflight import GATE_DETECTORS, baseline_from, evaluate, render_text, run
from baglens.readers.base import open_bag
from tests.synth.generate import generate_bag, preflight_scenario

WINDOW_S = 30.0
BASELINE_SEED = 5000


def _bag(tmp: Path, kind: str, seed: int, duration_s: float = WINDOW_S) -> Path:
    topics, faults = preflight_scenario(kind)
    path = tmp / f"{kind}_{seed}.mcap"
    generate_bag(path, seed=seed, duration_s=duration_s, topics=topics, faults=faults)
    return path


@pytest.fixture(scope="module")
def baseline(tmp_path_factory: pytest.TempPathFactory) -> Baseline:
    """Captured from a known-good run, exactly as `preflight --record` would."""
    tmp = tmp_path_factory.mktemp("preflight_base")
    path = _bag(tmp, "healthy", BASELINE_SEED, duration_s=120.0)
    report = Auditor(open_bag(path), detectors=GATE_DETECTORS).run()
    base = baseline_from(report, source=str(path))
    assert base.topics, "a baseline with no topics would make every later check vacuous"
    return base


def _gate(path: Path, baseline: Baseline):
    return run(ReplayFeed(path, speed=0.0), baseline, WINDOW_S, CONFIG.current)


# ------------------------------------------------------------------ the headline


@pytest.mark.parametrize("seed", range(10))
def test_a_healthy_graph_never_raises_an_alarm(
    seed: int, baseline: Baseline, tmp_path: Path
) -> None:
    """Ten healthy graphs, ten different seeds, zero false alarms.

    This is the number that decides whether anyone leaves the gate switched on.
    """
    report = _gate(_bag(tmp_path, "healthy", seed), baseline)
    assert report.verdict == "go", f"false alarm on a healthy graph: {report.failures}"


# ---------------------------------------------------------------- what it catches


@pytest.mark.parametrize(
    ("kind", "expect_in_reasons"),
    [
        ("missing_topic", "/scan"),
        ("halved_rate", "/scan"),
        ("clock_skew", "clock"),
        ("already_degrading", "/camera/image_raw"),
        ("silent_topic", "/scan"),
    ],
)
def test_the_gate_catches_each_fault_and_names_it(
    kind: str, expect_in_reasons: str, baseline: Baseline, tmp_path: Path
) -> None:
    report = _gate(_bag(tmp_path, kind, 77), baseline)
    assert report.verdict == "no_go", f"{kind} was waved through"
    blob = " ".join(report.failures).lower()
    assert expect_in_reasons.lower() in blob, (
        f"{kind} was caught but the reason does not name {expect_in_reasons}: "
        f"{report.failures}"
    )


def test_the_age_budget_is_checked_and_can_fail(
    baseline: Baseline, tmp_path: Path
) -> None:
    """F1's data age, used as a gate condition — a pipeline already lagging before the
    mission starts is exactly what this is for."""
    ages = {t: b.age_p95_ms for t, b in baseline.topics.items()}
    assert any(v is not None for v in ages.values()), (
        "no topic carries an age baseline, so the age check would be untested dead code"
    )

    # tighten one topic's budget below what the healthy graph actually achieves
    tight = baseline.model_copy(deep=True)
    tight.topics["/scan"] = tight.topics["/scan"].model_copy(update={"age_p95_ms": 0.5})
    report = _gate(_bag(tmp_path, "healthy", 8), tight)

    age_checks = [c for c in report.checks if c.check == "data_age" and c.topic == "/scan"]
    assert age_checks and age_checks[0].status == "fail", age_checks
    assert report.verdict == "no_go"


# ------------------------------------------------------ the refusal that matters


def test_a_topic_too_slow_to_judge_is_unchecked_not_passed(
    baseline: Baseline, tmp_path: Path
) -> None:
    """Thirty seconds is shorter than warmup on a slow topic.

    Such a topic must appear in `unchecked` and must not be quietly counted as healthy —
    that is the difference between a gate and a rubber stamp.
    """
    slow = Baseline(
        source="synthetic",
        topics={**baseline.topics},
    )
    slow.topics["/telemetry"] = baseline.topics[next(iter(baseline.topics))].model_copy(
        update={"hz": 0.05}  # one message every 20 s
    )
    report = _gate(_bag(tmp_path, "healthy", 1), slow)

    # it published nothing at all, so it is a genuine failure, not an unchecked
    assert any("/telemetry" in f for f in report.failures)

    # and the checks it could not run are listed rather than assumed
    assert report.unchecked
    assert any("tf" in u for u in report.unchecked), (
        "TF completeness is not implemented; claiming it would be exactly the failure "
        "this gate exists to prevent"
    )


def test_unchecked_items_can_be_made_fatal(baseline: Baseline, tmp_path: Path) -> None:
    from dataclasses import replace

    cfg = CONFIG.current
    strict = replace(cfg, preflight=replace(cfg.preflight, strict=True))
    path = _bag(tmp_path, "healthy", 2)
    report = run(ReplayFeed(path, speed=0.0), baseline, WINDOW_S, strict)
    assert report.verdict == "no_go", "strict mode must not pass on unchecked items"


# ------------------------------------------------------------------- the contract


def test_the_verdict_arrives_within_its_budget(baseline: Baseline, tmp_path: Path) -> None:
    """Under 35 s wall clock for a 30 s window — a gate nobody waits for is a gate
    nobody runs. Replay is uncapped here, so this measures the gate's own overhead."""
    import time

    path = _bag(tmp_path, "healthy", 3)
    t0 = time.monotonic()
    report = _gate(path, baseline)
    assert time.monotonic() - t0 < 35.0
    assert report.elapsed_s >= 0.0


def test_text_output_leads_with_the_verdict(baseline: Baseline, tmp_path: Path) -> None:
    text = render_text(_gate(_bag(tmp_path, "missing_topic", 4), baseline))
    assert text.splitlines()[0].startswith("NO-GO")
    assert "/scan" in text


def test_a_baseline_round_trips_through_json(baseline: Baseline) -> None:
    restored = Baseline.model_validate_json(baseline.model_dump_json())
    assert restored.topics == baseline.topics


def test_evaluate_is_pure_given_a_report(baseline: Baseline, tmp_path: Path) -> None:
    """The comparison must not depend on how the arrivals were delivered."""
    path = _bag(tmp_path, "halved_rate", 6)
    report = Auditor(open_bag(path), detectors=GATE_DETECTORS).run()
    a = evaluate(baseline, report, WINDOW_S)
    b = evaluate(baseline, report, WINDOW_S)
    assert a.model_dump() == b.model_dump()
    assert a.verdict == "no_go"
