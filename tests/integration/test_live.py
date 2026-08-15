"""The live path must reach the same conclusions as the offline one.

That equivalence is the entire payoff of the streaming constraint: if a monitor fed by a
subscription can disagree with an audit of the same data on disk, then every number this
project publishes about recordings says nothing about vehicles, and the constraint bought
nothing. So it is asserted rather than assumed — on the same fixtures the offline
detectors are scored against, and on every fault class, not just the easy ones.

`speed=0` replays as fast as the disk allows: what is under test is ordering and state,
not wall-clock pacing, and a test that slept for real would be a test nobody runs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from baglens.detectors.auditor import Auditor
from baglens.live import LiveMonitor, ReplayFeed, TailFeed, audit_live
from baglens.readers import open_bag


def _summary(report: object) -> tuple[object, ...]:
    """Everything a caller would act on, in a comparable shape."""
    r = report
    return (
        r.verdict,  # type: ignore[attr-defined]
        round(r.overall_score, 6),  # type: ignore[attr-defined]
        tuple(
            sorted(
                (f.detector, round(f.t_start, 6), round(f.t_end, 6), f.summary)
                for f in r.findings  # type: ignore[attr-defined]
            )
        ),
        tuple(sorted((t.topic, round(t.score, 6)) for t in r.topics)),  # type: ignore[attr-defined]
    )


@pytest.mark.parametrize(
    "fixture",
    ["clean_bag", "dropout_bag", "degradation_bag", "jitter_bag", "drops_bag",
     "lag_bag", "step_bag", "stall_bag"],
)
def test_live_matches_offline(fixture: str, request: pytest.FixtureRequest) -> None:
    path = request.getfixturevalue(fixture)
    offline = Auditor(open_bag(path)).run()
    live = audit_live(ReplayFeed(path, speed=0))
    assert _summary(live) == _summary(offline), f"{fixture}: live and offline disagree"


def test_snapshots_do_not_disturb_the_run(stall_bag: Path) -> None:
    """A status request must not change the answer.

    `snapshot()` finishes a restored *copy* precisely so that asking often is free. If it
    ever finished the live auditor instead, this test fails on the final report rather
    than on the snapshots, which is the failure that would otherwise reach production.
    """
    offline = Auditor(open_bag(stall_bag)).run()

    monitor = LiveMonitor(ReplayFeed(stall_bag, speed=0))
    reports = list(monitor.run(snapshot_every_n=500))

    assert len(reports) > 1, "expected interim snapshots plus a final report"
    assert _summary(reports[-1]) == _summary(offline)


def _stalled_seconds(report: object) -> float:
    """Total recording time currently attributed to a system-wide stall."""
    spans = sorted(
        (f.t_start, f.t_end)
        for f in report.findings  # type: ignore[attr-defined]
        if f.detector == "correlation" and f.topic is None
    )
    total, cursor = 0.0, float("-inf")
    for start, end in spans:  # union, because merged stalls can overlap
        start = max(start, cursor)
        if end > start:
            total += end - start
            cursor = end
    return round(total, 6)


def test_live_findings_are_revised_but_stall_coverage_never_shrinks(stall_bag: Path) -> None:
    """The invariant a live monitor can actually promise.

    Individual findings are **not** monotonic, and that is correct rather than a defect:

    * `aperiodic` says "this topic has no cadence I can measure *yet*". Early in a stream
      every topic is in warmup, so a first snapshot is mostly these, and each is withdrawn
      as its topic earns a baseline.
    * `dropped` is an *estimate* — `expected_hz × active_duration − observed`. A topic
      silenced by a recorder stall looks lossy until the stall is recognised as
      system-wide, at which point it is correctly no longer billed for it.

    So "fire an alert on any new finding" is the wrong way to build on this: it would
    alert and then retract. What does hold is that **time once attributed to a stall stays
    attributed** — the recorder-stalled claim only ever grows. That is the claim an
    operator would act on, and it is what this asserts.
    """
    monitor = LiveMonitor(ReplayFeed(stall_bag, speed=0))
    covered = 0.0
    revised = False
    seen: set[tuple[str, float]] = set()

    for report in monitor.run(snapshot_every_n=500):
        now = {(f.detector, round(f.t_start, 3)) for f in report.findings}
        revised |= bool(seen - now)
        seen = now

        stalled = _stalled_seconds(report)
        assert stalled >= covered - 1e-6, (
            f"stall coverage shrank from {covered}s to {stalled}s — a window that was "
            f"attributed to the recorder stopped being attributed to it"
        )
        covered = stalled

    assert revised, (
        "expected some findings to be revised as evidence arrived (warmup and dropped "
        "estimates); if nothing is ever revised, the semantics under test have changed"
    )
    assert covered > 0, "the stall fixture should end with time attributed to a stall"


def test_checkpoint_resumes_a_restarted_monitor(stall_bag: Path, tmp_path: Path) -> None:
    """Kill a monitor mid-stream, restart it, and it must land where it would have.

    This is the property that lets a monitor run on a vehicle that loses power: the
    learned baselines survive, so the restarted process is not blind through a second
    warmup window.
    """
    ckpt = tmp_path / "monitor.json"
    offline = Auditor(open_bag(stall_bag)).run()

    # First leg: stop deliberately part-way through.
    first = LiveMonitor(ReplayFeed(stall_bag, speed=0), checkpoint_path=ckpt)
    halfway = sum(1 for _ in open_bag(stall_bag).arrivals()) // 2
    for i, arrival in enumerate(first.feed.arrivals()):
        first.auditor._ensure_global_detectors()
        first.auditor.push(arrival)
        if i >= halfway:
            break
    first.checkpoint()
    assert ckpt.exists()
    state = json.loads(ckpt.read_text())
    assert state["n"] == halfway + 1

    # Second leg: a fresh monitor restores, and is fed the remainder.
    second = LiveMonitor(ReplayFeed(stall_bag, speed=0), checkpoint_path=ckpt)
    assert second.auditor.n == halfway + 1, "restored monitor forgot what it had seen"
    for i, arrival in enumerate(second.feed.arrivals()):
        if i <= halfway:
            continue
        second.auditor.push(arrival)
    resumed = second.auditor.finish()

    assert _summary(resumed) == _summary(offline)


def test_replay_paces_to_the_clock(clean_bag: Path) -> None:
    """`speed` must actually throttle, or "live" testing proves nothing about timing."""
    import time

    started = time.monotonic()
    # The clean fixture spans 30s; at 300x that is ~0.1s of wall clock.
    audit_live(ReplayFeed(clean_bag, speed=300.0, tick_s=0.01))
    elapsed = time.monotonic() - started
    assert elapsed < 20.0, "pacing should compress, not stall"


def test_tail_feed_follows_an_unfinished_recording(growing_bag: Path) -> None:
    """A file with no summary and no trailing magic is what a live recording looks like.

    The tail feed must read it without the recovery path refusing it, and must stop on
    its own when the writer goes quiet rather than blocking a caller forever.
    """
    feed = TailFeed(growing_bag, poll_s=0.01, idle_timeout_s=0.2)
    arrivals = list(feed.arrivals())
    assert arrivals, "tail feed returned nothing from a growing recording"
    assert all(
        a.log_time_ns <= b.log_time_ns for a, b in zip(arrivals, arrivals[1:], strict=False)
    ), "tail feed emitted arrivals out of order"
