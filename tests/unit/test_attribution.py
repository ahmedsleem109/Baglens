"""Stall attribution.

These are written against synthetic signals where the answer is known by construction,
because the whole value of this kernel is that it says "nothing explains this" when
nothing does. A kernel that always names a cause is worse than no kernel, so the
negative cases matter more than the positive one.
"""

from __future__ import annotations

import random

from baglens.kernels.attribution import (
    MAX_EFFECT,
    MIN_CONSISTENCY,
    MIN_EFFECT,
    PRE_WINDOW_S,
    StallAttributor,
    classify_pattern,
)


def _signal(attr: StallAttributor, name: str, values: list[tuple[float, float]]) -> None:
    for t, v in values:
        attr.feed(name, t, v)


def _flat(duration: float, value: float, hz: float = 5.0) -> list[tuple[float, float]]:
    n = int(duration * hz)
    return [(i / hz, value) for i in range(n)]


class TestFindsARealCause:
    def test_signal_that_spikes_before_every_stall_is_attributed(self) -> None:
        rng = random.Random(3)
        stalls = [(50.0, 51.0), (120.0, 121.5), (200.0, 201.0)]
        attr = StallAttributor(stalls, duration_s=300.0)

        # Real telemetry is noisy — a noiseless two-valued signal has zero within-group
        # variance and no defined effect size. Model the realistic case here; the
        # degenerate one is covered separately below.
        samples = []
        for i in range(int(300 * 5)):
            t = i / 5.0
            leading = any(lo - PRE_WINDOW_S <= t < lo for lo, _ in stalls)
            base = 0.90 if leading else 0.40
            samples.append((t, base + rng.gauss(0, 0.05)))
        _signal(attr, "cpuload.load", samples)

        rep = attr.report()
        assert rep.verdict == "attributed"
        assert rep.explained
        top = rep.attributions[0]
        assert top.signal == "cpuload.load"
        assert top.direction == "elevated"
        assert top.effect_size > MIN_EFFECT
        assert "cpuload.load" in rep.interpretation

    def test_perfect_separation_is_the_strongest_evidence_not_a_divide_by_zero(
        self,
    ) -> None:
        """A noiseless signal that is one value before stalls and another elsewhere is
        perfectly separated — which must rank at the top, not be silently dropped."""
        stalls = [(50.0, 51.0), (120.0, 121.0), (200.0, 201.0)]
        attr = StallAttributor(stalls, duration_s=300.0)
        for i in range(int(300 * 5)):
            t = i / 5.0
            leading = any(lo - PRE_WINDOW_S <= t < lo for lo, _ in stalls)
            attr.feed("cpuload.load", t, 0.90 if leading else 0.40)

        rep = attr.report()
        assert rep.verdict == "attributed"
        assert rep.attributions[0].effect_size == MAX_EFFECT

    def test_the_strongest_signal_is_ranked_first(self) -> None:
        rng = random.Random(11)
        stalls = [(50.0, 51.0), (120.0, 121.0), (200.0, 201.0)]
        attr = StallAttributor(stalls, duration_s=300.0)

        for i in range(int(300 * 5)):
            t = i / 5.0
            leading = any(lo - PRE_WINDOW_S <= t < lo for lo, _ in stalls)
            # Both real signals sit clearly above the noise floor — ranking two shifts
            # that are themselves within noise of each other would assert nothing.
            attr.feed("weak", t, 0.50 + (0.15 if leading else 0.0) + rng.gauss(0, 0.05))
            attr.feed("strong", t, 0.50 + (0.40 if leading else 0.0) + rng.gauss(0, 0.05))
            attr.feed("unrelated", t, 0.50 + rng.gauss(0, 0.05))

        rep = attr.report()
        assert [a.signal for a in rep.attributions][:2] == ["strong", "weak"]
        assert rep.attributions[0].explains
        assert rep.attributions[-1].signal == "unrelated"
        assert not rep.attributions[-1].explains


class TestReportsHonestlyWhenNothingExplains:
    def test_unrelated_signal_is_not_attributed(self) -> None:
        rng = random.Random(7)
        stalls = [(50.0, 51.0), (120.0, 121.0), (200.0, 201.0)]
        attr = StallAttributor(stalls, duration_s=300.0)
        _signal(
            attr,
            "cpuload.load",
            [(i / 5.0, 0.5 + rng.gauss(0, 0.05)) for i in range(int(300 * 5))],
        )

        rep = attr.report()
        assert rep.verdict == "unexplained"
        assert not rep.explained
        assert "No candidate signal explains" in rep.interpretation

    def test_a_signal_with_too_few_samples_is_not_ranked(self) -> None:
        attr = StallAttributor([(50.0, 51.0)], duration_s=300.0)
        _signal(attr, "sparse", [(10.0, 1.0), (20.0, 2.0), (30.0, 3.0)])
        rep = attr.report()
        assert rep.verdict == "no_data"
        assert rep.attributions == []

    def test_constant_signal_cannot_explain_anything(self) -> None:
        """Zero variance would divide by zero — and a constant explains nothing anyway."""
        attr = StallAttributor([(50.0, 51.0), (120.0, 121.0)], duration_s=300.0)
        _signal(attr, "constant", _flat(300.0, 4.2))
        rep = attr.report()
        assert rep.attributions == []
        assert rep.verdict == "no_data"


class TestConsistencyGuard:
    """An aggregate mean shift is not an explanation on its own.

    Testing a few signals across many recordings turns up |d| > 0.5 by chance
    routinely — on the public PX4 corpus those chance hits pointed in contradictory
    directions from log to log. A cause has to precede *most* stalls.
    """

    def test_shift_carried_by_one_outlying_stall_is_rejected(self) -> None:
        rng = random.Random(5)
        stalls = [(50.0 + 40.0 * i, 51.0 + 40.0 * i) for i in range(8)]
        attr = StallAttributor(stalls, duration_s=600.0)

        # Only the first stall is preceded by a spike; the other seven are not. The
        # aggregate mean moves, but the pattern does not recur.
        for i in range(int(600 * 5)):
            t = i / 5.0
            spike = stalls[0][0] - PRE_WINDOW_S <= t < stalls[0][0]
            attr.feed("cpuload.load", t, (4.0 if spike else 0.5) + rng.gauss(0, 0.05))

        rep = attr.report()
        top = rep.attributions[0]
        assert abs(top.effect_size) >= MIN_EFFECT, "expected a large aggregate shift"
        assert top.consistency < MIN_CONSISTENCY
        assert not top.explains
        assert rep.verdict == "unexplained"
        assert "close to chance" in rep.interpretation

    def test_consistent_shift_across_stalls_is_accepted(self) -> None:
        rng = random.Random(6)
        stalls = [(50.0 + 40.0 * i, 51.0 + 40.0 * i) for i in range(8)]
        attr = StallAttributor(stalls, duration_s=600.0)

        for i in range(int(600 * 5)):
            t = i / 5.0
            leading = any(lo - PRE_WINDOW_S <= t < lo for lo, _ in stalls)
            attr.feed("cpuload.load", t, (0.85 if leading else 0.5) + rng.gauss(0, 0.05))

        rep = attr.report()
        top = rep.attributions[0]
        assert top.consistency >= MIN_CONSISTENCY
        assert top.stalls_tested >= 3
        assert top.explains
        assert rep.verdict == "attributed"
        assert "% of the" in top.summary()

    def test_per_stall_tracking_is_bounded(self) -> None:
        stalls = [(10.0 * i, 10.0 * i + 0.2) for i in range(1, 900)]
        attr = StallAttributor(stalls, duration_s=10_000.0)
        for i in range(int(9000 * 5)):
            attr.feed("s", i / 5.0, float(i % 5))
        assert len(attr._signals["s"].per_stall) <= 512


class TestClusteringConfound:
    """The bug that produced a fake d=-5.9 in the analysis behind this kernel.

    Stalls cluster hard on real flights, so the window before one stall is routinely
    full of *other* stalls. If those samples are not excluded, the pre-window looks
    starved and every signal appears to collapse before a stall.
    """

    def test_samples_inside_neighbouring_stalls_are_excluded(self) -> None:
        # Ten stalls packed into ten seconds — every pre-window overlaps its neighbours.
        stalls = [(100.0 + i * 1.0, 100.0 + i * 1.0 + 0.5) for i in range(10)]
        attr = StallAttributor(stalls, duration_s=600.0)

        # The signal is *identical* everywhere it is actually observed. It only looks
        # different if stall-interior samples leak into the comparison.
        samples = []
        for i in range(int(600 * 10)):
            t = i / 10.0
            if any(lo <= t <= hi for lo, hi in stalls):
                samples.append((t, 0.0))  # nothing publishes: the trap
            else:
                samples.append((t, 0.60))
        _signal(attr, "cpuload.load", samples)

        rep = attr.report()
        assert rep.verdict != "attributed", (
            "stall-interior zeros leaked into the pre-window and faked a correlation"
        )
        for a in rep.attributions:
            assert abs(a.effect_size) < MIN_EFFECT


class TestPatternClassification:
    def test_clustered_stalls_are_recognised(self) -> None:
        # Three tight bursts separated by long quiet stretches.
        stalls = [
            (base + i * 0.5, base + i * 0.5 + 0.2)
            for base in (50.0, 300.0, 550.0)
            for i in range(8)
        ]
        pattern = classify_pattern(stalls, duration_s=900.0)
        assert pattern.kind == "clustered"
        assert pattern.dispersion > 2.0
        assert "bursts" in pattern.summary()
        assert "shared resource" in pattern.summary()

    def test_evenly_spaced_stalls_read_as_periodic(self) -> None:
        stalls = [(30.0 * i, 30.0 * i + 0.3) for i in range(1, 20)]
        pattern = classify_pattern(stalls, duration_s=600.0)
        assert pattern.kind == "periodic"
        assert "scheduled task" in pattern.summary()

    def test_too_few_stalls_is_not_guessed_at(self) -> None:
        assert classify_pattern([(1.0, 2.0)], duration_s=600.0).kind == "unknown"

    def test_pattern_counts_and_total_silence(self) -> None:
        stalls = [(10.0, 12.0), (50.0, 51.5)]
        pattern = classify_pattern(stalls, duration_s=600.0)
        assert pattern.count == 2
        assert pattern.total_silent_s == 3.5


class TestBoundedState:
    def test_state_does_not_grow_with_sample_count(self) -> None:
        """The streaming constraint: this has to survive on a device."""
        import sys

        attr = StallAttributor([(50.0, 51.0), (120.0, 121.0)], duration_s=1e6)
        for i in range(2000):
            attr.feed("s", i / 5.0, float(i % 7))
        small = sys.getsizeof(attr._signals["s"].ring)

        for i in range(2000, 200_000):
            attr.feed("s", i / 5.0, float(i % 7))
        large = sys.getsizeof(attr._signals["s"].ring)

        assert large == small, "ring buffer grew with the stream"
        assert len(attr._signals["s"].ring) <= 256
