"""When the report must refuse to answer.

The recording that motivated this is `nuway_stops`: a shuttle bus parked for its whole
recording, 70 of whose 110 topics are event-driven, published as `compromised` at score
0.0. Nothing about it was compromised. The tool simply could not tell, and said something
confident instead.

These tests pin the two halves of that: refusing when a recording cannot support a
verdict, and — the half that is much easier to break — *not* refusing on an ordinary
recording that happens to have a few quiet topics.
"""

from __future__ import annotations

from baglens.config import CONFIG
from baglens.detectors.assessability import assess, coverage_fraction
from baglens.detectors.timeline import TimelineAccumulator
from baglens.models import TopicHealth


def _topic(name: str, count: int, source: str = "modal") -> TopicHealth:
    return TopicHealth(topic=name, count=count, hz_source=source)  # type: ignore[arg-type]


def _timeline(topics: dict[str, list[float]]) -> TimelineAccumulator:
    tl = TimelineAccumulator()
    for topic, times in topics.items():
        for t in times:
            tl.push(topic, t)
    return tl


class TestOrdinaryRecordingsAreStillJudged:
    """The regression that would matter most: refusing to answer on healthy data."""

    def test_a_normal_recording_is_assessable(self) -> None:
        topics = [_topic(f"/t{i}", 1000) for i in range(6)]
        tl = _timeline({f"/t{i}": [j * 0.5 for j in range(240)] for i in range(6)})
        result = assess(topics, 120.0, tl)
        assert result.assessable
        assert result.confidence == 1.0
        assert result.reasons == []

    def test_a_few_event_driven_topics_do_not_trigger_a_refusal(self) -> None:
        """A PX4 flight carries a handful of these and is entirely assessable."""
        topics = [_topic(f"/t{i}", 5000) for i in range(8)]
        topics += [_topic(f"/event{i}", 6, "aperiodic") for i in range(4)]
        tl = _timeline({f"/t{i}": [j * 0.5 for j in range(240)] for i in range(8)})
        result = assess(topics, 120.0, tl)
        assert result.assessable, result.reasons

    def test_a_broken_but_measurable_recording_is_judged_not_refused(self) -> None:
        """Refusal is about measurability, never about health. A recording full of real
        faults must still get a verdict — otherwise the refusal path becomes a way to
        avoid saying anything bad."""
        topics = [_topic(f"/t{i}", 1000) for i in range(5)]
        tl = _timeline({f"/t{i}": [j * 0.5 for j in range(240)] for i in range(5)})
        assert assess(topics, 120.0, tl).assessable


class TestRefusal:
    def test_mostly_event_driven_recording_is_refused(self) -> None:
        topics = [_topic(f"/e{i}", 100, "aperiodic") for i in range(70)]
        topics += [_topic(f"/t{i}", 50) for i in range(3)]
        result = assess(topics, 300.0, None)
        assert not result.assessable
        assert any("measurable publication rate" in r for r in result.reasons)

    def test_assessable_topics_carrying_almost_no_traffic_is_refused(self) -> None:
        topics = [_topic("/fast", 200_000, "aperiodic"), _topic("/slow", 100)]
        result = assess(topics, 300.0, None)
        assert not result.assessable
        assert any("of the messages" in r for r in result.reasons)

    def test_a_recording_that_is_mostly_silence_is_refused(self) -> None:
        """`nuway_stops` opens with 113 of its first 131 seconds publishing nothing."""
        topics = [_topic("/a", 2000), _topic("/b", 2000)]
        # everything happens in the last eighth of the recording
        tl = _timeline({"/a": [110.0 + j * 0.1 for j in range(200)],
                        "/b": [110.0 + j * 0.1 for j in range(200)]})
        result = assess(topics, 131.0, tl)
        assert not result.assessable
        assert any("published during" in r for r in result.reasons)

    def test_a_recording_shorter_than_warmup_is_refused(self) -> None:
        topics = [_topic("/a", 300)]
        result = assess(topics, 4.0, None)
        assert not result.assessable
        assert any("shorter than the cadence warmup" in r for r in result.reasons)

    def test_an_empty_recording_is_refused_rather_than_scored(self) -> None:
        result = assess([], 0.0, None)
        assert not result.assessable
        assert result.confidence == 0.0


class TestConfidence:
    def test_confidence_reflects_the_weakest_link(self) -> None:
        """Not an average: one fatal shortfall must not be diluted by three healthy
        ratios, because the report is only as trustworthy as its worst input."""
        topics = [_topic(f"/t{i}", 1000) for i in range(6)]
        tl = _timeline({f"/t{i}": [j * 0.5 for j in range(240)] for i in range(6)})
        short = assess(topics, 5.0, tl)
        assert short.confidence == round(5.0 / CONFIG.assessability.min_duration_s, 3)

    def test_confidence_is_capped_at_one(self) -> None:
        topics = [_topic(f"/t{i}", 100_000) for i in range(20)]
        tl = _timeline({f"/t{i}": [j * 0.5 for j in range(240)] for i in range(20)})
        assert assess(topics, 100_000.0, tl).confidence == 1.0


class TestCoverage:
    def test_coverage_counts_only_assessable_topics(self) -> None:
        """An event-driven topic firing once a minute must not make that minute look
        observed — that is precisely how a parked robot passed for a monitored one."""
        tl = _timeline({"/quiet": [0.0, 30.0, 60.0, 90.0],
                        "/busy": [90.0 + j * 0.1 for j in range(100)]})
        with_both = coverage_fraction(tl, {"/quiet", "/busy"})
        busy_only = coverage_fraction(tl, {"/busy"})
        assert busy_only < with_both

    def test_no_matching_topics_is_zero_not_full(self) -> None:
        tl = _timeline({"/a": [1.0, 2.0]})
        assert coverage_fraction(tl, {"/nothing"}) == 0.0
