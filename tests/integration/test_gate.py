"""`baglens gate` — the training-data gate and its manifest.

The gate's job is to be *specific about rejections*. A dataset owner who is told "score
61" learns nothing and lowers the threshold; one who is told "3.2% of this episode fell
inside a recorder stall" fixes the recorder. These tests hold the tool to that: every
rejection carries a code and a reason, and the decision rule is exercised against real
generated faults rather than against hand-built report objects.
"""

from __future__ import annotations

import json
from pathlib import Path

from baglens.detectors import Auditor
from baglens.gate import Episode, GatePolicy, judge, render, run_gate
from baglens.readers import open_bag


def _report(path: Path):
    reader = open_bag(path)
    try:
        return Auditor(reader).run()
    finally:
        reader.close()


class TestDecisions:
    def test_a_clean_episode_is_accepted(self, clean_bag: Path) -> None:
        ep = judge(_report(clean_bag), GatePolicy())
        assert ep.decision == "accept", ep.reasons
        assert ep.reasons == []

    def test_a_truncated_episode_is_rejected_with_a_reason(self, truncated_bag: Path) -> None:
        ep = judge(_report(truncated_bag), GatePolicy())
        assert ep.decision == "reject"
        codes = {r["code"] for r in ep.reasons}
        assert codes & {"truncated", "corrupt"}, ep.reasons
        assert all(r["detail"] for r in ep.reasons)

    def test_a_stalled_episode_is_rejected_for_the_stall_specifically(
        self, stall_bag: Path
    ) -> None:
        """The reason code matters as much as the decision: a training set owner needs to
        know it is the recorder, not the sensor."""
        ep = judge(_report(stall_bag), GatePolicy(max_stall_fraction=0.001))
        assert ep.decision == "reject"
        assert "recorder_stall" in {r["code"] for r in ep.reasons}

    def test_a_missing_required_topic_is_rejected_by_name(self, clean_bag: Path) -> None:
        ep = judge(_report(clean_bag), GatePolicy(require_topics=("/gripper/state",)))
        assert ep.decision == "reject"
        reason = next(r for r in ep.reasons if r["code"] == "missing_topic")
        assert reason["evidence"]["topic"] == "/gripper/state"

    def test_message_loss_above_the_limit_is_rejected(self, drops_bag: Path) -> None:
        ep = judge(_report(drops_bag), GatePolicy(max_drop_fraction=0.01))
        assert ep.decision == "reject"
        assert "message_loss" in {r["code"] for r in ep.reasons}

    def test_the_same_loss_below_the_limit_is_accepted(self, drops_bag: Path) -> None:
        """The limit has to be a limit, or the gate is just a mood."""
        ep = judge(_report(drops_bag), GatePolicy(max_drop_fraction=0.95))
        assert "message_loss" not in {r["code"] for r in ep.reasons}

    def test_limits_apply_only_to_required_topics_when_named(self, drops_bag: Path) -> None:
        """A dataset that trains on two topics should not be blocked by a third one's
        diagnostics channel losing messages."""
        report = _report(drops_bag)
        lossy = max(report.topics, key=lambda t: t.estimated_dropped)
        others = [t.topic for t in report.topics if t.topic != lossy.topic]
        ep = judge(report, GatePolicy(max_drop_fraction=0.01,
                                      require_topics=tuple(others)))
        assert "message_loss" not in {r["code"] for r in ep.reasons}


class TestPolicyKnobs:
    def test_min_score_rejects_and_says_so(self, dropout_bag: Path) -> None:
        ep = judge(_report(dropout_bag), GatePolicy(min_score=100.0))
        assert ep.decision == "reject"
        assert "low_score" in {r["code"] for r in ep.reasons}

    def test_a_short_fragment_is_rejected(self, clean_bag: Path) -> None:
        report = _report(clean_bag)
        ep = judge(report, GatePolicy(min_duration_s=report.duration_s + 10))
        assert "too_short" in {r["code"] for r in ep.reasons}

    def test_an_absolute_gap_limit_catches_what_a_fraction_misses(
        self, dropout_bag: Path
    ) -> None:
        """The case that motivated the knob: a 15 s hole in a 31-minute recording is 0.8%
        and passes every fraction limit, while being fifteen seconds the policy never
        sees."""
        report = _report(dropout_bag)
        lenient = judge(report, GatePolicy(max_stall_fraction=1.0, max_drop_fraction=1.0))
        strict = judge(report, GatePolicy(max_stall_fraction=1.0, max_drop_fraction=1.0,
                                          max_gap_s=0.5))
        assert lenient.decision == "accept", lenient.reasons
        assert "gap" in {r["code"] for r in strict.reasons}

    def test_recorder_lag_is_rejected(self, lag_bag: Path) -> None:
        ep = judge(_report(lag_bag), GatePolicy(max_clock_lag_s=0.01))
        assert "clock_lag" in {r["code"] for r in ep.reasons}

    def test_flagging_never_downgrades_a_rejection(self) -> None:
        ep = Episode(path="x")
        ep.reject("truncated", "incomplete")
        ep.flag("compromised", "looks bad")
        assert ep.decision == "reject"
        assert len(ep.reasons) == 2


class TestManifest:
    def test_manifest_separates_safe_episodes_from_the_rest(
        self, bagdir: Path, clean_bag: Path, truncated_bag: Path
    ) -> None:
        manifest = run_gate(bagdir, GatePolicy())
        assert manifest["manifest_version"] == 1
        assert manifest["summary"]["episodes"] >= 2

        decisions = {Path(e["path"]).name: e["decision"] for e in manifest["episodes"]}
        assert decisions[clean_bag.name] == "accept"
        assert decisions[truncated_bag.name] == "reject"

        # the safe list is what a training job consumes, so it must never contain a
        # rejected episode by construction rather than by convention
        assert str(clean_bag) in manifest["train_on"]
        assert str(truncated_bag) not in manifest["train_on"]

    def test_every_rejection_carries_a_code_and_a_reason(self, bagdir: Path) -> None:
        manifest = run_gate(bagdir, GatePolicy())
        for episode in manifest["episodes"]:
            if episode["decision"] == "accept":
                continue
            assert episode["reasons"], episode["path"]
            for reason in episode["reasons"]:
                assert reason["code"] and reason["detail"]

    def test_the_manifest_records_the_policy_it_was_produced_under(
        self, bagdir: Path
    ) -> None:
        """A manifest without its policy is not reproducible, and a dataset owner cannot
        tell a stricter run from a worse dataset."""
        policy = GatePolicy(min_score=42.0, require_topics=("/odom",))
        manifest = run_gate(bagdir, policy)
        assert manifest["policy"]["min_score"] == 42.0
        assert manifest["policy"]["require_topics"] == ["/odom"]

    def test_manifest_is_json_serialisable(self, bagdir: Path) -> None:
        json.dumps(run_gate(bagdir, GatePolicy()))

    def test_garbage_files_are_rejected_rather_than_crashing(
        self, tmp_path: Path
    ) -> None:
        """A file of noise does not raise — the recovery path opens it and finds nothing,
        which is the reader behaving correctly. What matters here is that the gate turns
        that into a rejection with a structural reason rather than into a traceback or,
        worse, an empty and therefore "clean" episode."""
        bad = tmp_path / "not_really.mcap"
        bad.write_bytes(b"this is not an mcap file")
        manifest = run_gate(tmp_path, GatePolicy())
        assert manifest["summary"]["rejected"] == 1
        assert manifest["train_on"] == []
        codes = set(manifest["summary"]["rejections_by_code"])
        assert codes & {"unreadable", "truncated", "corrupt", "too_short"}, codes

    def test_render_summarises_without_crashing(self, bagdir: Path) -> None:
        text = render(run_gate(bagdir, GatePolicy()), verbose=True)
        assert "episodes under" in text
