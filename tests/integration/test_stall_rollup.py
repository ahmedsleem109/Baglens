"""One stall should be one finding.

When the recorder stops, every topic goes silent at once. Reporting that once per topic
turned an ordinary flight into 900–3000 findings and buried the single fact that mattered
under its own consequences — and then charged every topic for messages it was never given
the chance to publish.
"""

from __future__ import annotations

from pathlib import Path

from baglens.detectors.auditor import Auditor
from baglens.readers import open_bag

STALL_TOPICS = ("/imu/data", "/odom", "/scan", "/camera/image_raw")


def test_gaps_inside_a_stall_are_reported_once(stall_bag: Path) -> None:
    report = Auditor(open_bag(stall_bag)).run()

    stalls = [f for f in report.findings if f.detector == "correlation"]
    assert stalls, "the correlated stall must still be detected"

    # The per-topic silences inside the stall window are the same event seen N times.
    rolled = [
        f for f in report.findings
        if f.detector == "gap" and f.topic in STALL_TOPICS
        and any(s.t_start - 1 <= f.t_start and f.t_end <= s.t_end + 1 for s in stalls)
    ]
    assert rolled == [], f"per-topic gaps survived inside the stall: {rolled}"

    # Not discarded — moved onto the stall finding as evidence.
    absorbed = max(f.evidence.get("gaps_rolled_up", 0) for f in stalls)
    assert absorbed >= len(STALL_TOPICS) - 1


def test_topics_are_not_billed_for_a_shared_stall(stall_bag: Path) -> None:
    report = Auditor(open_bag(stall_bag)).run()
    for th in report.topics:
        if th.topic in STALL_TOPICS:
            assert th.stall_silent_s > 0, f"{th.topic} should record its share of the stall"
            assert th.estimated_dropped == 0, (
                f"{th.topic} was billed {th.estimated_dropped} dropped messages for a "
                "stall that silenced the whole recorder"
            )


def test_the_stall_still_costs_the_recording(stall_bag: Path, clean_bag: Path) -> None:
    """Rolling findings up must not make the stall free — the data really is missing."""
    stalled = Auditor(open_bag(stall_bag)).run()
    clean = Auditor(open_bag(clean_bag)).run()

    assert stalled.overall_score < clean.overall_score
    assert stalled.verdict != "trustworthy"
    assert any("system-wide" in c or "host" in c for c in stalled.caveats)


def test_an_isolated_dropout_is_untouched(dropout_bag: Path) -> None:
    """The guard against over-correcting: a single topic dying is that topic's fault and
    must still be reported per topic, with its dropped messages counted."""
    report = Auditor(open_bag(dropout_bag)).run()

    gaps = [f for f in report.findings if f.detector == "gap" and f.topic == "/scan"]
    assert gaps, "an isolated dropout must still produce a per-topic gap finding"

    scan = next(t for t in report.topics if t.topic == "/scan")
    assert scan.stall_silent_s == 0.0
    assert scan.estimated_dropped > 0
    assert report.verdict == "usable_with_caveats"


def test_finding_count_stays_proportionate(stall_bag: Path) -> None:
    """A stall across four topics should not produce a finding per topic per detector."""
    report = Auditor(open_bag(stall_bag)).run()
    assert len(report.findings) <= 4, (
        f"one stall produced {len(report.findings)} findings: "
        f"{[(f.detector, f.topic) for f in report.findings]}"
    )
