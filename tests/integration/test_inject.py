"""Fault injection into an existing recording.

The injector is a measuring instrument, so the thing to test is that it does exactly what
its labels claim and nothing else. A label that overstates its fault turns a detector
failure into a detector success, silently, and there is nothing downstream that would
catch it — the eval trusts the sidecar completely.

The source here is a generated bag rather than a real one, because CI has no real
recordings. That tests the mechanism; `evals/integrity/INJECTED.md` is what tests it
against real jitter.
"""

from __future__ import annotations

import json
from pathlib import Path

from baglens.detectors import Auditor
from baglens.readers import open_bag
from tests.synth.generate import topic_dropout
from tests.synth.inject import inject, periodic_topics, place, profile_window, summarise


def _times(path: Path, topic: str) -> list[float]:
    reader = open_bag(path)
    try:
        arrivals = sorted(a.log_time_ns for a in reader.arrivals() if a.topic == topic)
    finally:
        reader.close()
    t0 = arrivals[0]
    return [(t - t0) / 1e9 for t in arrivals]


class TestTheCopyIsFaithful:
    def test_a_copy_with_no_faults_keeps_every_message(
        self, clean_bag: Path, tmp_path: Path
    ) -> None:
        truth = inject(clean_bag, tmp_path / "copy.mcap", [])
        before = summarise(clean_bag)
        assert sum(t["count"] for t in truth["topics"]) == before["message_count"]

    def test_the_topic_inventory_survives_even_an_emptied_topic(
        self, clean_bag: Path, tmp_path: Path
    ) -> None:
        """The correlation denominator reads the declared topic list, so a topic that
        loses all its messages must still be declared — otherwise the injection has
        quietly changed the recording's shape beyond the fault."""
        info = summarise(clean_bag)
        topic = info["topics"][0]["topic"]
        dest = tmp_path / "emptied.mcap"
        inject(clean_bag, dest, [topic_dropout(topic, 0.0, 1e6)])
        assert {t["topic"] for t in summarise(dest)["topics"]} == {
            t["topic"] for t in info["topics"]
        }

    def test_the_copy_is_in_log_time_order(self, clean_bag: Path, tmp_path: Path) -> None:
        """A jitter kick or a backward step can push a message behind its neighbour; the
        reorder heap is what keeps the file readable."""
        from tests.synth.generate import clock_step

        dest = tmp_path / "stepped.mcap"
        inject(clean_bag, dest, [clock_step(30.0, 1500.0, "backward")])
        reader = open_bag(dest)
        try:
            times = [a.log_time_ns for a in reader.arrivals()]
        finally:
            reader.close()
        assert times == sorted(times)


class TestLabelsAreHonest:
    def test_a_dropout_removes_exactly_its_window(
        self, clean_bag: Path, tmp_path: Path
    ) -> None:
        info = summarise(clean_bag)
        topic = info["topics"][0]["topic"]
        dest = tmp_path / "dropout.mcap"
        truth = inject(clean_bag, dest, [topic_dropout(topic, 30.0, 8.0)])

        inside = [t for t in _times(dest, topic) if 30.0 <= t < 38.0]
        assert inside == [], "messages survived inside the labelled window"
        fault = truth["faults"][0]
        assert fault["effective"]
        assert fault["messages_affected"] > 0

    def test_a_fault_that_changes_nothing_is_marked_void(
        self, clean_bag: Path, tmp_path: Path
    ) -> None:
        """The failure this field exists for: a dropout planned from whole-file rates
        landed on a bursty topic's quiet stretch, removed nothing, and every detector
        took a free miss on a fault that was never injected."""
        dest = tmp_path / "void.mcap"
        truth = inject(clean_bag, dest, [topic_dropout("/nonexistent", 10.0, 5.0)])
        assert truth["faults"][0]["effective"] is False
        assert truth["faults"][0]["messages_affected"] == 0

    def test_ground_truth_sidecar_matches_the_generator_schema(
        self, clean_bag: Path, tmp_path: Path
    ) -> None:
        """`evals/integrity/run.py` reads both corpora with one scorer, which only works
        while the two sidecars agree on their field names."""
        dest = tmp_path / "sidecar.mcap"
        inject(clean_bag, dest, [topic_dropout("/odom", 20.0, 4.0)])
        truth = json.loads(dest.with_suffix(".ground_truth.json").read_text())
        for key in ("path", "duration_s", "topics", "faults", "clean"):
            assert key in truth
        assert truth["clean"] is False
        assert truth["faults"][0]["kind"] == "topic_dropout"

    def test_the_injected_dropout_is_actually_detected(
        self, clean_bag: Path, tmp_path: Path
    ) -> None:
        """End to end, on the mechanism at least: injected fault in, finding out."""
        dest = tmp_path / "detected.mcap"
        inject(clean_bag, dest, [topic_dropout("/odom", 40.0, 8.0)])
        reader = open_bag(dest)
        try:
            report = Auditor(reader).run()
        finally:
            reader.close()
        gaps = [f for f in report.findings if f.detector == "gap" and f.topic == "/odom"]
        assert any(38.0 <= f.t_start <= 42.0 for f in gaps), [f.summary for f in gaps]


class TestPlanningRefusesWhatItCannotLabel:
    def test_place_finds_a_window_where_the_topic_is_live(self) -> None:
        live = {"/a": set(range(0, 10)) | set(range(40, 60))}
        assert place(5.0, 60.0, live, ("/a",), 20.0) == 40.0

    def test_place_returns_none_when_no_window_exists(self) -> None:
        live = {"/a": {0, 5, 10, 15}}  # never two consecutive seconds
        assert place(5.0, 60.0, live, ("/a",), 0.0) is None

    def test_a_bursty_topic_is_not_offered_as_a_fault_target(self) -> None:
        info = {"topics": [{"topic": "/burst", "hz": 50.0, "coverage": 0.01},
                           {"topic": "/steady", "hz": 10.0, "coverage": 0.99}]}
        assert periodic_topics(info) == ["/steady"]

    def test_a_slow_topic_is_not_offered_either(self) -> None:
        """A 0.2 Hz topic cannot host a detectable gap: the hole is shorter than its own
        period, so the label would be asking for something impossible."""
        info = {"topics": [{"topic": "/slow", "hz": 0.2, "coverage": 1.0}]}
        assert periodic_topics(info) == []

    def test_window_profiling_measures_the_window_not_the_file(
        self, clean_bag: Path
    ) -> None:
        info = summarise(clean_bag)
        t0 = info["start_time_ns"]
        half = t0 + int(info["duration_s"] * 1e9 / 2)
        window = profile_window(clean_bag, t0, half)
        assert window["message_count"] < info["message_count"]
        assert window["duration_s"] < info["duration_s"]
