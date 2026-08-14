"""Topics that cannot support a rate model, and the score that must ignore them.

Every real PX4 flight carries a handful of event-driven topics — `/home_position` with
five messages, `/vehicle_command_ack` with seven, `/event` arriving in bursts. Learning a
period from those and then measuring them against it produced thousands of phantom gaps,
100% drop rates, and a topic score of zero that set `min()` for the entire report. That is
most of why every real recording read as `compromised`.

The rule these tests pin down: if we cannot establish a baseline, we say so, and we do
not let the resulting non-measurement decide the verdict.
"""

from __future__ import annotations

from baglens.config import CONFIG
from baglens.detectors.auditor import _sustained_hz, _unassessable_reason
from baglens.detectors.cadence import TopicCadence
from baglens.detectors.score import overall_score
from baglens.models import TopicHealth


def _cadence(times: list[float]) -> TopicCadence:
    cad = TopicCadence("/t", CONFIG.cadence)
    for t in times:
        cad.push(t)
    return cad


class TestSparseTopics:
    def test_a_topic_with_five_messages_is_not_assessable(self) -> None:
        cad = _cadence([0.0, 40.0, 120.0, 250.0, 380.0])
        assert not cad.ready
        assert _unassessable_reason(cad, CONFIG.current) == "sparse"

    def test_two_messages_do_not_yield_a_rate_model(self) -> None:
        """The `/sensor_selection` case: 2 messages 0.7s apart once produced a 1.4 Hz
        baseline and a claim that 482 messages were missing."""
        cad = _cadence([0.72, 1.43])
        assert _unassessable_reason(cad, CONFIG.current) == "sparse"

    def test_a_warmed_up_topic_is_assessable(self) -> None:
        cad = _cadence([i * 0.1 for i in range(900)])  # 10 Hz for 90s
        assert cad.ready
        assert _unassessable_reason(cad, CONFIG.current) is None


class TestAperiodicTopics:
    def test_bursty_topic_is_rejected_even_though_warmup_completed(self) -> None:
        """`/event`: bursts of near-simultaneous messages separated by long silences.
        The modal inter-arrival is the spacing *inside* a burst, ~1000x the real rate."""
        times = [burst * 30.0 + i * 0.001 for burst in range(6) for i in range(10)]
        cad = _cadence(times)
        assert cad.ready, "expected warmup to complete — that is what makes this case hard"
        assert (cad.expected_hz or 0) > 100, "modal rate should be wildly high"
        assert _sustained_hz(cad) < 1.0
        assert _unassessable_reason(cad, CONFIG.current) == "aperiodic"

    def test_a_genuine_dropout_is_not_mistaken_for_aperiodicity(self) -> None:
        """The regression that would matter most: silencing real gap detection.

        A 10 Hz topic with an 8s hole must stay assessable, or the gap detector goes
        quiet on exactly the fault it exists to find.
        """
        times = [i * 0.1 for i in range(400)]
        times += [40.0 + 8.0 + i * 0.1 for i in range(400)]
        cad = _cadence(times)
        assert cad.ready
        assert _unassessable_reason(cad, CONFIG.current) is None

    def test_a_topic_silent_for_half_its_life_is_still_assessable(self) -> None:
        """Ratio ~2 — well under the 5.0 threshold, so a badly broken but real topic is
        still measured rather than excused."""
        times = [i * 0.1 for i in range(450)] + [90.0 + i * 0.1 for i in range(450)]
        cad = _cadence(times)
        assert _unassessable_reason(cad, CONFIG.current) is None


class TestOverallScoreIgnoresUnassessableTopics:
    def _topic(self, name: str, score: float, source: str = "modal") -> TopicHealth:
        return TopicHealth(topic=name, score=score, hz_source=source)  # type: ignore[arg-type]

    def test_an_aperiodic_topic_does_not_set_the_minimum(self) -> None:
        healthy = [self._topic(f"/t{i}", 95.0) for i in range(5)]
        with_junk = [*healthy, self._topic("/event", 0.0, "aperiodic")]
        assert overall_score(with_junk, 100.0) == overall_score(healthy, 100.0)

    def test_a_real_bad_topic_still_sets_the_minimum(self) -> None:
        healthy = [self._topic(f"/t{i}", 95.0) for i in range(5)]
        with_bad = [*healthy, self._topic("/scan", 10.0)]
        assert overall_score(with_bad, 100.0) < overall_score(healthy, 100.0)

    def test_all_aperiodic_falls_back_rather_than_dividing_by_zero(self) -> None:
        only_junk = [self._topic("/event", 50.0, "aperiodic")]
        assert overall_score(only_junk, 100.0) > 0


class TestStallPenalty:
    def _topics(self, stall_s: float) -> list[TopicHealth]:
        return [TopicHealth(topic=f"/t{i}", score=100.0, stall_silent_s=stall_s)
                for i in range(4)]

    def test_a_stall_costs_the_report_once(self) -> None:
        clean = overall_score(self._topics(0.0), 100.0, duration_s=90.0)
        stalled = overall_score(self._topics(6.0), 100.0, duration_s=90.0)
        assert stalled < clean

    def test_a_longer_stall_costs_more(self) -> None:
        light = overall_score(self._topics(6.0), 100.0, duration_s=90.0)
        heavy = overall_score(self._topics(45.0), 100.0, duration_s=90.0)
        assert heavy < light

    def test_the_penalty_is_not_multiplied_by_topic_count(self) -> None:
        """The bug this replaced: every topic was charged for the same shared event, so
        a 100-topic robot was punished 100 times for one stall."""
        four = overall_score(self._topics(6.0), 100.0, duration_s=90.0)
        many = overall_score(
            [TopicHealth(topic=f"/t{i}", score=100.0, stall_silent_s=6.0) for i in range(100)],
            100.0,
            duration_s=90.0,
        )
        assert abs(four - many) < 1e-6
