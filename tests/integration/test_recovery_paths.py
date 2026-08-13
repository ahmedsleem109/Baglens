"""The damaged-file paths: corruption, an unfinished recording, and what they cost.

Every branch here was written before any fixture could reach it. Two real bugs were
sitting in that gap: a corrupt chunk in a file with an intact summary was invisible,
and a freshly written file was reported as "still growing" because its mtime was recent.
"""

from __future__ import annotations

from pathlib import Path

from baglens.detectors import Auditor
from baglens.readers import open_bag, validate_file


def test_corrupt_chunk_is_detected_despite_an_intact_summary(corrupt_bag: Path) -> None:
    """The index promises messages the data no longer delivers. That gap is the damage."""
    fi = validate_file(corrupt_bag)
    assert fi.readable
    assert fi.has_summary  # the summary survived; only the chunk is damaged
    assert fi.partial
    assert fi.messages_claimed > fi.messages_readable > 0
    assert fi.unreadable_fraction > 0.1
    assert any(issue.kind == "crc_mismatch" for issue in fi.chunk_issues)


def test_corruption_costs_the_file_its_score(corrupt_bag: Path) -> None:
    fi = validate_file(corrupt_bag)
    assert fi.score < 70, "a file whose second half does not decode is not healthy"


def test_corrupt_recording_cannot_be_called_trustworthy(corrupt_bag: Path) -> None:
    """Integrity caps the verdict: the surviving half is internally consistent, and
    weighting it against a healthy topic table would let a corrupt file score well."""
    report = Auditor(open_bag(corrupt_bag)).run()
    assert report.verdict == "compromised"
    assert any(f.detector == "file_integrity" for f in report.findings)
    assert any("do not decode" in c for c in report.caveats)


def test_unfinished_recording_recovers_by_scan(growing_bag: Path) -> None:
    fi = validate_file(growing_bag)
    assert fi.readable
    assert not fi.has_summary
    assert fi.partial
    assert fi.last_readable_time is not None


def test_unfinished_recording_still_audits(growing_bag: Path) -> None:
    report = Auditor(open_bag(growing_bag)).run()
    assert report.topics, "recovery should still yield per-topic health"
    assert report.provenance.sample_count > 100
    assert report.provenance.partial


def test_closed_file_is_never_reported_as_in_progress(clean_bag: Path) -> None:
    """A valid summary and trailing magic mean the writer closed the file, however
    recently it was written."""
    fi = validate_file(clean_bag)
    assert fi.has_summary
    assert not fi.in_progress


def test_clean_file_scores_full_marks(clean_bag: Path) -> None:
    fi = validate_file(clean_bag)
    assert fi.score == 100.0
    assert fi.chunk_issues == []
    assert fi.messages_claimed == fi.messages_readable


def test_garbage_file_is_reported_not_raised(tmp_path: Path) -> None:
    target = tmp_path / "junk.mcap"
    target.write_bytes(b"this is not an MCAP file at all" * 100)
    fi = validate_file(target)
    assert not fi.readable
    assert fi.score == 0.0
    assert any("magic" in note for note in fi.notes)


def test_empty_file_is_reported_not_raised(tmp_path: Path) -> None:
    target = tmp_path / "empty.mcap"
    target.write_bytes(b"")
    fi = validate_file(target)
    assert not fi.readable
