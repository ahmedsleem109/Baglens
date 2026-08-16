"""F1 — the rules that are not obvious by inspection.

Twice on this project a test caught a defect that reading the code did not; the
integer-nanosecond one below is the third. In float seconds, a stage that stamps with its
own publish time lands at -1e-16, is discarded as "stamped in the future", and half the
pipeline's links silently disappear — while every other test stays green.
"""

from __future__ import annotations

from baglens.config import CONFIG
from baglens.detectors.age import DataAgeDetector

NS = 1_000_000_000
T0 = 1_780_000_000 * NS


def feed(det: DataAgeDetector, rows: list[tuple[str, float, float]]) -> None:
    """(topic, publish seconds, stamp seconds), both relative to the bag start."""
    for topic, pub_s, stamp_s in rows:
        pub_ns = T0 + int(pub_s * NS)
        stamp_ns = T0 + int(stamp_s * NS)
        det.on_arrival(topic, pub_s, pub_ns, stamp_ns, T0, "sensor_msgs/msg/Imu")


def test_age_is_publish_minus_stamp() -> None:
    det = DataAgeDetector()
    feed(det, [("/a", 1.0 + i * 0.1, 1.0 + i * 0.1 - 0.05) for i in range(50)])
    stage = next(s for s in det.report()["stages"] if s["topic"] == "/a")
    assert abs(stage["age_p50_ms"] - 50.0) < 1.0


def test_a_stage_that_stamps_at_publish_still_links_downstream() -> None:
    """The integer-nanosecond regression. A zero age must not be read as a negative one."""
    rows = []
    for i in range(200):
        t = i * 0.05
        rows.append(("/mid", t, t))          # restamps: stamp == its own publish time
        rows.append(("/end", t + 0.02, t))   # carries it forward
    det = DataAgeDetector()
    feed(det, rows)
    end = next(s for s in det.report()["stages"] if s["topic"] == "/end")
    assert end["upstream"] == "/mid"
    assert end["link_observations"] == 200, "every message should have linked"


def test_the_propagation_graph_is_inferred_from_stamp_equality() -> None:
    rows = []
    for i in range(200):
        t = i * 0.05
        rows.append(("/cam", t + 0.01, t))
        rows.append(("/det", t + 0.09, t))
        rows.append(("/cmd", t + 0.13, t))
    det = DataAgeDetector()
    feed(det, rows)
    edges = {s["topic"]: s["upstream"] for s in det.report()["stages"]}
    assert edges == {"/cam": None, "/det": "/cam", "/cmd": "/det"}


def test_stage_delay_is_measured_from_the_immediate_upstream() -> None:
    rows = []
    for i in range(200):
        t = i * 0.05
        rows.append(("/cam", t + 0.01, t))
        rows.append(("/det", t + 0.09, t))
    det = DataAgeDetector()
    feed(det, rows)
    det_stage = next(s for s in det.report()["stages"] if s["topic"] == "/det")
    assert abs(det_stage["age_p50_ms"] - 90.0) < 2.0
    assert abs(det_stage["stage_p50_ms"] - 80.0) < 2.0


def test_an_unstamped_topic_is_named_not_given_an_age() -> None:
    """The rule the whole feature's honesty rests on."""
    det = DataAgeDetector()
    for i in range(100):
        det.on_arrival("/cmd_vel", i * 0.05, T0 + int(i * 0.05 * NS), None, T0,
                       "geometry_msgs/msg/Twist")
    report = det.report()
    assert "/cmd_vel" in report["unmeasurable"]
    assert all(s["topic"] != "/cmd_vel" for s in report["stages"])


def test_an_unset_stamp_is_not_an_age_of_fifty_five_years() -> None:
    det = DataAgeDetector()
    for i in range(100):
        det.on_arrival("/a", i * 0.05, T0 + int(i * 0.05 * NS), 0, T0, "x")
    findings = det.finalize(10.0)
    assert any("never sets it" in f.summary for f in findings)
    assert all(s["topic"] != "/a" for s in det.report()["stages"])


def test_a_stamp_ahead_of_its_publish_is_reported_not_averaged_in() -> None:
    det = DataAgeDetector()
    feed(det, [("/a", i * 0.05, i * 0.05 + 0.2) for i in range(100)])
    findings = det.finalize(10.0)
    assert any("stamped ahead" in f.summary for f in findings)


def test_a_stamp_on_another_clock_is_refused_not_reported_as_an_age() -> None:
    """Found on a real recording, not imagined.

    `/bond` on the nuway shuttle-bus bag stamps from a steady clock that starts near
    zero, which differences into an age of 54 years. Averaged into a report next to a
    48 ms lidar age, it makes every honest number there look arbitrary.
    """
    det = DataAgeDetector()
    for i in range(200):
        pub_ns = T0 + int(i * 0.05 * NS)
        det.on_arrival("/bond", i * 0.05, pub_ns, i * 1_000_000, T0, "bond/msg/Status")
    findings = det.finalize(10.0)
    assert any("different clock" in f.summary for f in findings)
    assert "/bond" in det.report()["unmeasurable"]
    assert all(s["topic"] != "/bond" for s in det.report()["stages"])


def test_a_growing_age_is_caught_and_a_flat_one_is_not() -> None:
    def run(ramp: bool) -> list[str]:
        det = DataAgeDetector()
        rows = []
        for i in range(4000):
            t = i * 0.05
            age = 0.05 + (0.4 * t / 200.0 if ramp else 0.0)
            rows.append(("/a", t, t - age))
        feed(det, rows)
        return [f.summary for f in det.finalize(200.0) if "age is growing" in f.summary]

    assert run(True), "a 9x growth in age over 200 s must be caught"
    assert not run(False), "a flat age must not fire"


def test_a_ramp_that_finished_early_is_still_caught() -> None:
    """The bug the real-background eval found, and the reason it is scored on real data.

    The bucket ring holds 30 buckets — 300 s. A pipeline that degrades over the first
    half of a ten-minute mission and then sits at its new, worse level has scrolled
    entirely out of that window by the end: the visible buckets are flat, the Theil-Sen
    slope is ~0, and the detector reported nothing. Recall on real recordings was 0.222
    while synthetic recall stayed at 1.000, because the synthetic runs were short enough
    that the ramp never left the window.

    The mission's opening P99 is frozen in two floats that outlive the ring, so the
    comparison survives.
    """
    det = DataAgeDetector()
    rows = []
    for i in range(24_000):          # 1200 s at 20 Hz
        t = i * 0.05
        # 50 ms, ramping to 200 ms between t=150 and t=450, flat thereafter
        if t <= 150:
            age = 0.05
        elif t >= 450:
            age = 0.20
        else:
            age = 0.05 + 0.15 * (t - 150) / 300.0
        rows.append(("/a", t, t - age))
    feed(det, rows)
    findings = [f for f in det.finalize(1200.0) if "age is growing" in f.summary]
    assert findings, "a 4x degradation that plateaued must still be reported"
    assert "opening P99" in findings[0].rule


def test_a_topic_whose_age_never_moves_stays_silent_over_a_long_run() -> None:
    """The precision half of the rule above: a long flat run must not fire."""
    det = DataAgeDetector()
    feed(det, [("/a", i * 0.05, i * 0.05 - 0.05) for i in range(24_000)])
    assert not [f for f in det.finalize(1200.0) if "age is growing" in f.summary]


def test_a_sparse_topic_gets_its_age_but_not_a_verdict_on_its_trend() -> None:
    """W15's lesson, applied to a new detector.

    A P99 computed from four samples is a maximum, not a statistic. Without a floor on
    samples per bucket, `nuway_stops` — the parked shuttle bus whose topics are
    event-driven — produced **16** false "data age is growing" findings, one of them
    claiming a 57x rise on a topic publishing 0.4 Hz. The age itself is still reported;
    only the trend is refused.
    """
    det = DataAgeDetector()
    # 0.4 Hz for 600 s: plenty of buckets, four samples in each
    rows = []
    for i in range(240):
        t = i * 2.5
        age = 0.01 if t < 300 else 0.9      # a violent, obvious "growth"
        rows.append(("/sparse", t, t - age))
    feed(det, rows)

    assert not [f for f in det.finalize(600.0) if "age is growing" in f.summary]
    stage = next(s for s in det.report()["stages"] if s["topic"] == "/sparse")
    assert stage["trend_assessable"] is False
    assert stage["age_p50_ms"] > 0, "the age is still measured — only the trend is refused"


def test_a_dense_topic_with_the_same_growth_is_still_caught() -> None:
    """The other half: the floor must not silence topics that can support the test."""
    det = DataAgeDetector()
    rows = []
    for i in range(60_000):
        t = i * 0.01                         # 100 Hz for 600 s
        age = 0.01 if t < 300 else 0.9
        rows.append(("/dense", t, t - age))
    feed(det, rows)
    assert [f for f in det.finalize(600.0) if "age is growing" in f.summary]


def test_findings_are_withheld_when_the_clocks_disagree() -> None:
    """An age measured across two unsynchronised clocks is that disagreement plus an
    unknown. Publishing it with a caveat invites someone to read past the caveat."""
    det = DataAgeDetector()
    rows = [("/a", i * 0.05, i * 0.05 - 0.05 - 0.4 * i / 4000) for i in range(4000)]
    feed(det, rows)
    det.clock_suspect = True
    findings = det.finalize(200.0)
    assert len(findings) == 1
    assert "not reported" in findings[0].summary


def test_state_is_bounded_by_message_count() -> None:
    """The constraint that must not be violated, stated as a test.

    State rises until the stamp table reaches its cap and then stops: a hundredfold more
    messages must cost nothing. Asserting a *constant* would be wrong — the table is
    allowed to fill — so what is asserted is that it saturates.
    """
    def state_after(n: int) -> int:
        det = DataAgeDetector()
        feed(det, [("/a", i * 0.01, i * 0.01 - 0.05) for i in range(n)])
        return det.state_bytes()

    saturated = state_after(20_000)
    assert state_after(2_000_000) == saturated
    assert saturated < 400_000, "one topic must not cost hundreds of kilobytes"


def test_a_checkpoint_round_trips() -> None:
    det = DataAgeDetector()
    rows = []
    for i in range(500):
        t = i * 0.05
        rows.append(("/cam", t + 0.01, t))
        rows.append(("/det", t + 0.09, t))
    feed(det, rows)
    restored = DataAgeDetector.from_state(det.to_state(), CONFIG.current)
    assert restored.report() == det.report()


def test_a_split_pass_reaches_the_same_report_as_one_pass() -> None:
    """The streaming constraint, stated as a test: a checkpoint mid-stream must not
    change the answer."""
    rows = []
    for i in range(600):
        t = i * 0.05
        rows.append(("/cam", t + 0.01, t))
        rows.append(("/det", t + 0.09, t))

    whole = DataAgeDetector()
    feed(whole, rows)

    first = DataAgeDetector()
    feed(first, rows[: len(rows) // 2])
    second = DataAgeDetector.from_state(first.to_state(), CONFIG.current)
    feed(second, rows[len(rows) // 2 :])

    assert second.report() == whole.report()
