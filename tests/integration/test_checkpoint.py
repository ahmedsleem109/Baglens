"""Checkpoint and restore.

The README and the design notes both describe detector state as "a fixed-size struct,
serialisable, so it can be checkpointed". The sizes were asserted long before the
serialisation existed. These tests are what makes that sentence true.

The bar is deliberately high: a restored auditor must reach findings *identical* to one
that never stopped. Anything weaker — "similar", "same count" — would let a detector
quietly restart an episode, re-derive a baseline, or double-count a window, which are
exactly the bugs a checkpoint introduces.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from baglens.detectors.auditor import Auditor
from baglens.models import HealthReport
from baglens.readers import open_bag


def _comparable(report: HealthReport) -> list[dict[str, Any]]:
    """Findings stripped of the fields that legitimately differ between runs."""
    out = []
    for f in report.findings:
        d = f.model_dump(mode="json")
        d.pop("provenance", None)
        d.pop("id", None)
        out.append(d)
    return out


def _split_audit(path: Path, fraction: float, roundtrip_json: bool = True) -> HealthReport:
    """Audit ``path`` in two halves with a checkpoint between them."""
    reader = open_bag(path)
    arrivals = list(reader.arrivals())
    cut = int(len(arrivals) * fraction)

    first = Auditor(reader)
    first._ensure_global_detectors()
    for a in arrivals[:cut]:
        first.push(a)

    state = first.to_state()
    if roundtrip_json:
        # Through actual JSON, not just a dict copy: a checkpoint that only survives in
        # memory is not a checkpoint. This also catches tuple/deque/set leakage.
        state = json.loads(json.dumps(state))

    second = Auditor.from_state(state, open_bag(path))
    for a in arrivals[cut:]:
        second.push(a)
    return second.finish()


@pytest.mark.parametrize("fraction", [0.25, 0.5, 0.75])
def test_split_recording_matches_single_pass(dropout_bag: Path, fraction: float) -> None:
    """The headline claim: split anywhere, get the same findings."""
    whole = Auditor(open_bag(dropout_bag)).run()
    resumed = _split_audit(dropout_bag, fraction)

    # Without this the equality below would still pass if both sides found nothing,
    # which is the way a test like this rots.
    assert whole.findings, "nothing detected — the comparison would be vacuous"
    assert _comparable(resumed) == _comparable(whole)
    assert resumed.overall_score == whole.overall_score
    assert resumed.verdict == whole.verdict


@pytest.mark.parametrize(
    "bag_fixture",
    ["clean_bag", "dropout_bag", "degradation_bag", "jitter_bag", "drops_bag",
     "lag_bag", "step_bag", "stall_bag"],
)
def test_every_fault_class_survives_a_checkpoint(
    bag_fixture: str, request: pytest.FixtureRequest
) -> None:
    """Each detector has its own in-flight state — an open gap, a live degradation
    episode, a jitter excursion, an EWMA lag curve. Every one of them has to cross the
    checkpoint, so every fault class gets split down the middle."""
    path = request.getfixturevalue(bag_fixture)
    whole = Auditor(open_bag(path)).run()
    resumed = _split_audit(path, 0.5)

    assert _comparable(resumed) == _comparable(whole)
    assert [t.model_dump(mode="json") for t in resumed.topics] == [
        t.model_dump(mode="json") for t in whole.topics
    ]


def test_checkpoint_is_json_serialisable(stall_bag: Path) -> None:
    """Bounded state is only useful if it can leave the process."""
    reader = open_bag(stall_bag)
    auditor = Auditor(reader)
    auditor._ensure_global_detectors()
    for a in list(reader.arrivals())[:5000]:
        auditor.push(a)

    blob = json.dumps(auditor.to_state())
    assert json.loads(blob)["version"] == 1


def test_checkpoint_size_is_bounded_per_topic(degradation_bag: Path) -> None:
    """A checkpoint that grows with the recording would defeat the whole constraint.
    Compare the state after a quarter of the stream with the state after all of it."""
    reader = open_bag(degradation_bag)
    arrivals = list(reader.arrivals())

    def size_after(n: int) -> int:
        a = Auditor(open_bag(degradation_bag))
        a._ensure_global_detectors()
        for arr in arrivals[:n]:
            a.push(arr)
        return len(json.dumps(a.to_state()))

    quarter = size_after(len(arrivals) // 4)
    whole = size_after(len(arrivals))

    # Not equal — the gap list and the timeline legitimately accumulate — but it must
    # not scale with message count. 4x the messages must not mean 2x the state.
    assert whole < 2 * quarter, f"checkpoint grew {quarter} -> {whole} bytes with the stream"


def test_rejects_unknown_checkpoint_version(clean_bag: Path) -> None:
    reader = open_bag(clean_bag)
    state = Auditor(reader).to_state()
    state["version"] = 99
    with pytest.raises(ValueError, match="unsupported auditor checkpoint version"):
        Auditor.from_state(state, reader)
