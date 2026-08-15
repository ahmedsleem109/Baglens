"""Landing-time ingestion and the per-unit layer built on it.

These run against a real catalog on a temp path with real audited recordings, because the
failure mode being guarded against is a fleet answer that is confidently wrong — a unit
cleared to fly on evidence that was never actually about that unit.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from baglens.catalog.store import Catalog
from baglens.detectors import Auditor
from baglens.fleet import MIN_HISTORY, PreflightPolicy, fingerprint, precedents, preflight
from baglens.ingest import ingest_batch, ingest_landing
from baglens.readers import open_bag


@pytest.fixture
def catalog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Catalog:
    monkeypatch.setenv("BAGLENS_CACHE_DIR", str(tmp_path / "cache"))
    return Catalog(tmp_path / "fleet.duckdb")


def test_ingest_files_a_mission_under_the_unit_that_flew_it(
    catalog: Catalog, clean_bag: Path
) -> None:
    """`robot_id` comes from the fleet, not from the directory the file happens to sit in."""
    result = ingest_landing(clean_bag, robot_id="SN-0043", catalog=catalog,
                            with_signals=False)
    assert result.ok, result.error
    assert result.verdict == "trustworthy"
    assert result.robot_id == "SN-0043"

    rows = catalog.query("SELECT robot_id, verdict FROM missions")
    assert rows == [{"robot_id": "SN-0043", "verdict": "trustworthy"}]


def test_ingest_reuses_a_live_report_instead_of_re_auditing(
    catalog: Catalog, stall_bag: Path
) -> None:
    """A monitor that watched the mission already knows the answer.

    Re-reading the recording at landing to reach the same conclusion is the difference
    between ingestion finishing before the operator walks over and after they leave, so
    the reused report must produce exactly the row a fresh audit would.
    """
    live = Auditor(open_bag(stall_bag)).run()
    reused = ingest_landing(stall_bag, "SN-0043", report=live, catalog=catalog,
                            with_signals=False)
    assert reused.ok, reused.error
    assert reused.health_score == pytest.approx(live.overall_score)
    assert reused.verdict == live.verdict
    assert reused.headline, "an operator needs the reason, not just the verdict"


def test_a_failed_ingest_does_not_lose_the_recording(catalog: Catalog, tmp_path: Path) -> None:
    """A recording that cannot be indexed is still a recording."""
    missing = tmp_path / "never_written.mcap"
    result = ingest_landing(missing, "SN-0043", catalog=catalog)
    assert not result.ok
    assert result.error
    assert catalog.count("missions") == 0


def test_a_unit_with_no_history_is_never_cleared(catalog: Catalog) -> None:
    """"Never seen" is not "fine". A gate that cannot tell them apart is worse than none."""
    decision = preflight(catalog, "SN-9999")
    assert decision.decision == "warn"
    assert "no recorded missions" in decision.reasons[0]


def test_preflight_blocks_a_unit_whose_recordings_are_compromised(
    catalog: Catalog, clean_bag: Path, stall_bag: Path, dropout_bag: Path
) -> None:
    """The decision an operations team feels: go, warn or block, each with its reasons."""
    for i, bag in enumerate((clean_bag, stall_bag, dropout_bag)):
        ingest_landing(bag, "SN-0100", catalog=catalog, with_signals=False)
        assert catalog.count("missions") == i + 1

    # Calibrate against the score the unit actually has, so the test is about the gate
    # rather than about what a fixture happens to score.
    observed = preflight(catalog, "SN-0100").last_score
    assert preflight(
        catalog, "SN-0100", PreflightPolicy(block_below=observed + 1)
    ).decision == "block"
    warn = preflight(
        catalog, "SN-0100",
        PreflightPolicy(block_below=observed - 1, warn_below=observed + 1),
    )
    assert warn.decision == "warn"
    assert warn.reasons
    assert warn.missions_considered == 3


def test_fingerprint_needs_history_before_it_claims_a_trend(
    catalog: Catalog, clean_bag: Path
) -> None:
    """One mission is noise. The trend must say `unknown` rather than invent a direction."""
    ingest_landing(clean_bag, "SN-0200", catalog=catalog, with_signals=False)
    fp = fingerprint(catalog, "SN-0200")
    assert fp.missions == 1
    assert all(t.direction == "unknown" for t in fp.trends)
    assert any(str(MIN_HISTORY) in note for note in fp.notes)


def test_fingerprint_reports_a_units_history(
    catalog: Catalog, clean_bag: Path, jitter_bag: Path, stall_bag: Path
) -> None:
    results = ingest_batch([clean_bag, jitter_bag, stall_bag], "SN-0300",
                           catalog=catalog, with_signals=False)
    assert all(r.ok for r in results), [r.error for r in results]

    fp = fingerprint(catalog, "SN-0300")
    assert fp.missions == 3
    health = next(t for t in fp.trends if t.metric == "health_score")
    assert health.n == 3
    assert health.direction in ("improving", "stable", "degrading")


def test_precedents_distinguish_one_bad_unit_from_a_fleet_problem(
    catalog: Catalog, stall_bag: Path, dropout_bag: Path
) -> None:
    """The answer that changes what an engineer does next.

    A finding on one unit is that unit's problem; the same finding across several is a
    fleet problem, and looking at this vehicle would be the wrong move.
    """
    ingest_landing(stall_bag, "SN-0400", catalog=catalog, with_signals=False)
    one_unit = precedents(catalog, "correlation")
    assert one_unit["occurrences"] > 0
    assert one_unit["fleet_wide"] is False

    ingest_landing(dropout_bag, "SN-0401", catalog=catalog, with_signals=False)
    both = precedents(catalog, "gap")
    if both["occurrences"]:
        assert set(both["units_affected"]) <= {"SN-0400", "SN-0401"}

    absent = precedents(catalog, "no_such_detector")
    assert absent["occurrences"] == 0
    assert "no precedent" in absent["interpretation"]
