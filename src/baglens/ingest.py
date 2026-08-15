"""Landing-time ingestion — the hinge between monitoring a vehicle and knowing a fleet.

On touchdown: audit the mission, then put it in the catalog under the unit that flew it.
No new detectors and no new analysis; what this adds is the two things the fleet layer
cannot work without.

**Identity.** Everything in Phase 3 — a unit's trend, a pre-flight decision, "has this
happened before" — is a question about a *named vehicle*. The catalog's fallback guesses
the robot from the directory name, which is right often enough for a laptop full of
downloaded logs and wrong on a vehicle, where the answer must come from the fleet, not
the filesystem. So `robot_id` is an argument here, and recorded as given.

**Not auditing twice.** A monitor that watched the mission already holds the report. At a
gigabyte a mission, re-reading the file at landing to reach the same conclusion is the
difference between ingestion finishing before the operator walks over and finishing after
they have gone home. `ingest_landing` takes the live report and reuses it.

Failure here must not lose the mission. A recording that cannot be indexed is still a
recording, so ingestion reports what went wrong and leaves the file alone rather than
raising into whatever ran it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .catalog.indexer import index_mission
from .catalog.store import Catalog


@dataclass
class IngestResult:
    """What ingestion did, in the shape an operator or an agent would act on."""

    path: str
    ok: bool
    mission_id: str = ""
    robot_id: str = ""
    verdict: str = ""
    health_score: float = 0.0
    duration_s: float = 0.0
    #: findings worth an operator's attention, most severe first
    headline: list[str] = field(default_factory=list)
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path, "ok": self.ok, "mission_id": self.mission_id,
            "robot_id": self.robot_id, "verdict": self.verdict,
            "health_score": round(self.health_score, 2),
            "duration_s": round(self.duration_s, 2),
            "headline": self.headline, "error": self.error,
        }


def _headline(report: Any, limit: int = 3) -> list[str]:
    worst = sorted(report.findings, key=lambda f: (-int(f.severity), f.t_start))
    return [f.summary for f in worst[:limit]]


def ingest_landing(
    path: str | Path,
    robot_id: str,
    report: Any = None,
    catalog: Catalog | None = None,
    with_signals: bool = True,
) -> IngestResult:
    """Audit (or reuse an audit of) one finished mission and file it under `robot_id`.

    Pass `report` when a live monitor already produced one for this recording — the
    result is identical, because live and offline are the same code path, and it costs a
    catalog write instead of a re-read.
    """
    p = Path(path)
    cat = catalog or Catalog()
    try:
        if report is None:
            from .detectors import Auditor
            from .readers import open_bag

            reader = open_bag(p)
            try:
                report = Auditor(reader).run()
            finally:
                reader.close()
        mission_id = index_mission(
            p, cat, with_signals=with_signals, report=report, robot_id=robot_id
        )
    except Exception as exc:  # noqa: BLE001 - a failed ingest must not lose the recording
        return IngestResult(path=str(p), ok=False, robot_id=robot_id,
                            error=f"{type(exc).__name__}: {exc}")

    return IngestResult(
        path=str(p),
        ok=True,
        mission_id=mission_id,
        robot_id=robot_id,
        verdict=report.verdict,
        health_score=report.overall_score,
        duration_s=report.duration_s,
        headline=_headline(report),
    )


def ingest_batch(
    paths: list[str | Path],
    robot_id: str,
    catalog: Catalog | None = None,
    with_signals: bool = True,
) -> list[IngestResult]:
    """Ingest several missions for one unit — a vehicle that landed with a backlog.

    One failure does not stop the rest: the point of a backlog is that it drains.
    """
    cat = catalog or Catalog()
    return [ingest_landing(p, robot_id, catalog=cat, with_signals=with_signals) for p in paths]
