"""Every format the README claims, executed end to end.

The readers for `.db3` and `.bag` were written long before anything opened a real file
of either kind. These tests exist so the claim and the code cannot drift apart again:
the same recording is converted to each format and must audit to the same conclusions.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from baglens.detectors import Auditor
from baglens.readers import open_bag
from tests.synth import generate as g


def audit(path: Path):
    reader = open_bag(path)
    try:
        return Auditor(reader).run()
    finally:
        reader.close()


# -- rosbag2 SQLite ----------------------------------------------------------


def test_db3_metadata(db3_bag: Path) -> None:
    reader = open_bag(db3_bag)
    meta = reader.metadata()
    assert meta.format == "db3"
    assert meta.message_count > 1000
    assert {t.topic for t in meta.topics} >= {"/odom", "/imu/data", "/scan"}
    assert all(t.msg_type for t in meta.topics)
    reader.close()


def test_db3_arrivals_match_the_count(db3_bag: Path) -> None:
    reader = open_bag(db3_bag)
    assert sum(1 for _ in reader.arrivals()) == reader.metadata().message_count
    reader.close()


def test_db3_decodes_numeric_fields(db3_bag: Path) -> None:
    reader = open_bag(db3_bag)
    values = [v for _t, v in reader.numeric_field("/odom", "twist.twist.linear.x")]
    assert len(values) > 100
    assert 0.3 < sum(values) / len(values) < 0.8
    reader.close()


def test_db3_audits_clean(db3_bag: Path) -> None:
    report = audit(db3_bag)
    assert report.verdict == "trustworthy"
    assert report.findings == []


# -- ROS 1 -------------------------------------------------------------------


def test_bag1_metadata(bag1_bag: Path) -> None:
    reader = open_bag(bag1_bag)
    meta = reader.metadata()
    assert meta.format == "bag1"
    assert meta.message_count > 1000
    assert {t.topic for t in meta.topics} >= {"/odom", "/imu/data"}
    reader.close()


def test_bag1_audits_clean(bag1_bag: Path) -> None:
    report = audit(bag1_bag)
    assert report.verdict == "trustworthy"
    assert report.findings == []


def test_bag1_decodes_numeric_fields(bag1_bag: Path) -> None:
    reader = open_bag(bag1_bag)
    values = [v for _t, v in reader.numeric_field("/odom", "twist.twist.linear.x")]
    assert len(values) > 100
    reader.close()


# -- PX4 ULog ----------------------------------------------------------------


def test_ulg_metadata(ulg_bag: Path) -> None:
    reader = open_bag(ulg_bag)
    meta = reader.metadata()
    assert meta.format == "ulog"
    assert meta.message_count > 1000
    assert {t.topic for t in meta.topics} >= {"/odom", "/imu_data", "/scan"}
    assert any("single timestamp" in w for w in meta.warnings)
    reader.close()


def test_ulg_arrivals_match_the_count(ulg_bag: Path) -> None:
    reader = open_bag(ulg_bag)
    assert sum(1 for _ in reader.arrivals()) == reader.metadata().message_count
    reader.close()


def test_ulg_audits_clean(ulg_bag: Path) -> None:
    report = audit(ulg_bag)
    assert report.verdict == "trustworthy"
    assert report.findings == []


def test_ulg_dropout_records_are_readable(ulg_dropout_bag: Path) -> None:
    """The logger's own dropout records are the ground truth in `evals/integrity`.

    They are dated from the last data message before them, so a scale mismatch between
    the two would place every label outside the recording — and the eval would read as a
    detector failure rather than a fixture bug.
    """
    reader = open_bag(ulg_dropout_bag)
    meta = reader.metadata()
    marks = reader._open().dropouts
    assert [d.duration for d in marks] == [600, 400]
    offsets = [d.timestamp / 1e6 - meta.start_time_ns / 1e9 for d in marks]
    assert offsets == pytest.approx([40.0, 41.0], abs=0.05)
    reader.close()


# -- the four agree ----------------------------------------------------------


def test_all_formats_reach_the_same_conclusions(clean_bag: Path, db3_bag: Path,
                                                bag1_bag: Path, ulg_bag: Path) -> None:
    """Same recording, four containers. A format-specific bug shows up here."""
    reports = {fmt: audit(p) for fmt, p in
               (("mcap", clean_bag), ("db3", db3_bag), ("bag1", bag1_bag), ("ulog", ulg_bag))}
    counts = {fmt: r.provenance.sample_count for fmt, r in reports.items()}
    assert len(set(counts.values())) == 1, counts

    rates = {
        fmt: {t.topic: round(t.observed_hz, 1) for t in r.topics}
        for fmt, r in reports.items()
    }
    for fmt in ("db3", "bag1", "ulog"):
        for topic, hz in rates["mcap"].items():
            # ULog names a dataset after the uORB struct, so `/imu/data` is `/imu_data`
            key = topic if fmt != "ulog" else "/" + g.ulog_name(topic)
            assert rates[fmt][key] == pytest.approx(hz, rel=0.02), (fmt, topic)


def test_db3_has_no_publish_time_and_says_so(db3_bag: Path) -> None:
    """A single-timestamp format cannot measure recorder lag; the report must not pretend."""
    report = audit(db3_bag)
    assert report.clock is not None
    assert report.clock.lag_growth_s == 0.0
    assert any("single timestamp" in w for w in report.clock.provenance.warnings)


def test_unsupported_extension_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "recording.rosbag"
    target.write_bytes(b"nope")
    with pytest.raises(ValueError, match="unsupported"):
        open_bag(target)


def test_rosbag2_directory_resolves_to_its_inner_file(db3_bag: Path) -> None:
    reader = open_bag(db3_bag.parent)
    assert reader.metadata().message_count > 0
    reader.close()
