"""One test per detector: inject the fault, assert the detector finds it and nothing else does.

Written against bags whose ground truth we control exactly, because a detector
validated against a fixture written afterwards is validated against its own assumptions.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from baglens.detectors import Auditor
from baglens.models import Severity
from baglens.readers import open_bag


def audit(path: Path):
    return Auditor(open_bag(path)).run()


def detectors_fired(report) -> set[str]:
    return {f.detector for f in report.findings}


# -- D1 cadence --------------------------------------------------------------


def test_cadence_learns_rates_without_configuration(clean_bag: Path) -> None:
    report = audit(clean_bag)
    by_topic = {t.topic: t for t in report.topics}
    for topic, hz in [("/imu/data", 100.0), ("/odom", 50.0), ("/scan", 10.0),
                      ("/camera/image_raw", 30.0), ("/cmd_vel", 20.0)]:
        assert by_topic[topic].expected_hz == pytest.approx(hz, rel=0.05), topic


def test_qos_deadline_is_preferred_when_it_agrees(clean_bag: Path) -> None:
    report = audit(clean_bag)
    imu = next(t for t in report.topics if t.topic == "/imu/data")
    assert imu.hz_source == "qos"


def test_clean_bag_produces_no_findings(clean_bag: Path) -> None:
    """False positives on healthy data are the failure mode that kills trust."""
    report = audit(clean_bag)
    assert report.findings == []
    assert report.verdict == "trustworthy"
    assert report.overall_score > 95.0


# -- D2 gaps -----------------------------------------------------------------


def test_gap_detected_at_the_right_place(dropout_bag: Path) -> None:
    report = audit(dropout_bag)
    gaps = [f for f in report.findings if f.detector == "gap" and f.topic == "/scan"]
    assert len(gaps) == 1
    g = gaps[0]
    assert g.t_start == pytest.approx(40.0, abs=1.0)
    assert g.t_end == pytest.approx(48.0, abs=1.0)
    assert g.severity >= Severity.MEDIUM
    assert g.evidence["estimated_lost_messages"] == pytest.approx(80, rel=0.15)


def test_gap_does_not_fire_on_other_topics(dropout_bag: Path) -> None:
    report = audit(dropout_bag)
    assert {f.topic for f in report.findings if f.detector == "gap"} == {"/scan"}


def test_gap_storage_is_bounded() -> None:
    from baglens.detectors.cadence import TopicCadence
    from baglens.detectors.gaps import GapDetector

    cad = TopicCadence("/t")
    det = GapDetector("/t", cad)
    t = 0.0
    for _ in range(60):  # warm up at 10 Hz
        t += 0.1
        cad.push(t)
    for _ in range(5000):
        for _ in range(10):  # ten healthy messages, so the next gap is not merged in
            t += 0.1
            det.on_arrival(t, cad.push(t))
        t += 3.0
        det.on_arrival(t, cad.push(t))
    assert len(det.gaps()) <= det.cfg.gap.max_gaps
    assert det.dropped_gaps > 0
    assert det.state_bytes() < 64_000


# -- D3 rate degradation -----------------------------------------------------


def test_rate_degradation_detected(degradation_bag: Path) -> None:
    report = audit(degradation_bag)
    hits = [f for f in report.findings if f.detector == "rate_degradation"]
    assert any(f.topic == "/odom" for f in hits)
    assert len(hits) == len({f.topic for f in hits}), "one drifting topic is one finding"
    f = next(f for f in hits if f.topic == "/odom")
    assert f.evidence["relative_slope"] < 0  # it slowed
    assert abs(f.evidence["relative_slope"]) > 0.15
    assert f.evidence["hz_at_end"] < f.evidence["hz_at_start"]
    # The sentence must agree with the two rates it prints. It did not: a real Tesla CAN
    # bus produced "sped up by 65% (1715.1 → 1650.3 Hz)", because the direction came from
    # the episode's peak slope while the rates came from a ring that had moved on.
    assert "slowed" in f.summary, f.summary


def test_rate_degradation_ignores_a_single_gap(dropout_bag: Path) -> None:
    """Theil-Sen exists precisely so one dropout does not read as a trend."""
    report = audit(dropout_bag)
    assert not [
        f for f in report.findings if f.detector == "rate_degradation" and f.topic == "/scan"
    ]


# -- D4 jitter ---------------------------------------------------------------


def test_jitter_expansion_detected(jitter_bag: Path) -> None:
    report = audit(jitter_bag)
    hits = [f for f in report.findings if f.detector == "jitter" and f.topic == "/imu/data"]
    assert hits
    f = hits[0]
    assert f.evidence["peak_cv"] > f.evidence["baseline_cv"] * 2
    assert f.t_start == pytest.approx(40.0, abs=15.0)


def test_jitter_reported_for_every_topic_regardless(clean_bag: Path) -> None:
    report = audit(clean_bag)
    assert all(t.jitter_cv > 0 for t in report.topics)
    assert all(t.jitter_cv < 0.1 for t in report.topics)


# -- D5 dropped --------------------------------------------------------------


def test_diffuse_drops_estimated(drops_bag: Path) -> None:
    report = audit(drops_bag)
    cam = next(t for t in report.topics if t.topic == "/camera/image_raw")
    expected = 0.2 * 90 * 30
    assert cam.estimated_dropped == pytest.approx(expected, rel=0.35)
    finding = next(
        f for f in report.findings if f.detector == "dropped" and f.topic == "/camera/image_raw"
    )
    assert finding.evidence["count_based"] > 0


def test_dropped_disagreement_signals_diffuse_loss(drops_bag: Path) -> None:
    report = audit(drops_bag)
    cam = next(t for t in report.topics if t.topic == "/camera/image_raw")
    # diffuse drops leave no gaps, so the two estimators must disagree
    assert cam.dropped_confidence < 0.5


def test_no_phantom_drops_on_clean_data(clean_bag: Path) -> None:
    report = audit(clean_bag)
    assert all(t.estimated_dropped == 0 for t in report.topics)


# -- D6 clock ----------------------------------------------------------------


def test_recorder_lag_growth_detected(lag_bag: Path) -> None:
    report = audit(lag_bag)
    hits = [f for f in report.findings if f.detector == "clock_lag"]
    assert hits
    assert report.clock is not None
    assert report.clock.lag_growth_s > 0.1
    assert len(report.clock.lag_curve_t) <= 100
    assert report.clock.lag_curve_s[-1] > report.clock.lag_curve_s[0]


def test_clock_step_detected_at_the_publish_instant(step_bag: Path) -> None:
    report = audit(step_bag)
    hits = [f for f in report.findings if f.detector in ("clock_step", "clock")]
    assert hits
    assert any(abs(f.t_start - 45.0) < 2.0 for f in hits)


def test_clock_step_is_reported_once_not_once_per_topic(step_bag: Path) -> None:
    report = audit(step_bag)
    steps = [f for f in report.findings if f.detector == "clock_step"]
    assert len(steps) <= 2


def test_clean_bag_clock_is_monotonic(clean_bag: Path) -> None:
    report = audit(clean_bag)
    assert report.clock is not None
    assert report.clock.monotonic
    assert report.clock.backward_jumps == 0


# -- D7 correlation ----------------------------------------------------------


def test_correlated_stall_classified_as_system_wide(stall_bag: Path) -> None:
    report = audit(stall_bag)
    hits = [f for f in report.findings if f.detector == "correlation"]
    assert hits
    stall = max(hits, key=lambda f: f.evidence.get("concurrency", 0))
    assert stall.evidence["concurrency"] > 0.7
    assert stall.t_start == pytest.approx(40.0, abs=2.0)


def test_isolated_dropout_is_not_called_a_stall(dropout_bag: Path) -> None:
    report = audit(dropout_bag)
    assert not [
        f
        for f in report.findings
        if f.detector == "correlation" and f.evidence.get("concurrency", 0) > 0.7
    ]


def test_gap_details_carry_co_silent_topics(stall_bag: Path) -> None:
    auditor = Auditor(open_bag(stall_bag))
    auditor.run()
    details = auditor.correlation.classify(auditor.all_gaps())
    stall = [d for d in details if d.classification == "system_wide_stall"]
    assert stall
    assert len(stall[0].co_silent_topics) >= 2


# -- D8 file integrity -------------------------------------------------------


def test_truncated_file_degrades_instead_of_raising(truncated_bag: Path) -> None:
    report = audit(truncated_bag)
    assert report.file_integrity is not None
    fi = report.file_integrity
    assert fi.partial or fi.truncated_bytes > 0
    assert not fi.has_summary
    assert fi.last_readable_time is not None
    assert any(f.detector == "file_integrity" for f in report.findings)


def test_truncation_produces_a_caveat(truncated_bag: Path) -> None:
    report = audit(truncated_bag)
    assert any("incomplete" in c for c in report.caveats)


def test_missing_file_is_reported_not_raised(tmp_path: Path) -> None:
    from baglens.readers import validate_file

    fi = validate_file(tmp_path / "nope.mcap")
    assert not fi.readable
    assert fi.score == 0.0


# -- the constraint ----------------------------------------------------------


def test_detector_state_is_bounded(clean_bag: Path) -> None:
    """The whole design: bounded state per topic, checkpointable, device-portable."""
    auditor = Auditor(open_bag(clean_bag))
    auditor.run()
    for topic, state in auditor.states.items():
        assert state.state_bytes() < 3300, topic


def test_edge_profile_fits_the_device_budget(clean_bag: Path, monkeypatch) -> None:
    from dataclasses import replace

    from baglens.config import CONFIG, CadenceConfig, DegradationConfig, JitterConfig

    edge = replace(
        CONFIG.current,
        cadence=CadenceConfig(hist_bins=48, ring_size=32),
        jitter=JitterConfig(window=96),
        degradation=DegradationConfig(n_buckets=24),
    )
    auditor = Auditor(open_bag(clean_bag), cfg=edge)
    auditor.run()
    for topic, state in auditor.states.items():
        assert state.state_bytes() < 2048, topic


def test_audit_makes_exactly_one_pass(clean_bag: Path, monkeypatch) -> None:
    reader = open_bag(clean_bag)
    calls = {"n": 0}
    original = reader.arrivals

    def counting(*a, **k):
        calls["n"] += 1
        return original(*a, **k)

    monkeypatch.setattr(reader, "arrivals", counting)
    Auditor(reader).run()
    assert calls["n"] == 1
