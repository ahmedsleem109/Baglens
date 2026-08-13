"""`catalog.*` — the corpus layer. Index once, query forever.

This is what turns a bag reader into a fleet tool: every question below is answered
from DuckDB in milliseconds without reopening a single recording.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from ..budget import apply_budget
from ..catalog import indexer as _indexer
from ..catalog.indexer import discover, index_in_background, index_paths
from ..catalog.store import Catalog
from ..models import Budgeted
from ..provenance import Provenance
from .common import resolve

_CATALOG: Catalog | None = None


def catalog() -> Catalog:
    global _CATALOG
    if _CATALOG is None:
        _CATALOG = Catalog()
    return _CATALOG


class SourceAdded(BaseModel):
    root: str
    files_found: int
    already_indexed: int
    indexing_started: bool
    total_duration_s: float = 0.0
    note: str = ""


class IndexProgress(BaseModel):
    running: bool
    total: int
    done: int
    failed: int
    progress: float
    elapsed_s: float
    eta_s: float
    current: str = ""
    errors: list[str] = Field(default_factory=list)


class MissionRow(BaseModel):
    mission_id: str
    path: str
    robot_id: str | None = None
    start_time: str | None = None
    duration_s: float = 0.0
    message_count: int = 0
    health_score: float = 100.0
    verdict: str = ""
    tags: list[str] = Field(default_factory=list)


class MissionList(Budgeted):
    missions: list[MissionRow] = Field(default_factory=list)
    total: int = 0
    provenance: Provenance = Field(default_factory=Provenance)


class MissionInfo(BaseModel):
    mission: MissionRow | None = None
    topics: list[dict[str, Any]] = Field(default_factory=list)
    events: list[dict[str, Any]] = Field(default_factory=list)
    signals: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
    provenance: Provenance = Field(default_factory=Provenance)


class FleetSummary(BaseModel):
    missions: int = 0
    robots: int = 0
    total_duration_h: float = 0.0
    total_size_gb: float = 0.0
    total_messages: int = 0
    verdicts: dict[str, int] = Field(default_factory=dict)
    mean_health: float = 0.0
    worst_missions: list[MissionRow] = Field(default_factory=list)
    common_topics: list[dict[str, Any]] = Field(default_factory=list)
    top_event_kinds: list[dict[str, Any]] = Field(default_factory=list)
    provenance: Provenance = Field(default_factory=Provenance)


def _row(r: dict[str, Any], tags: dict[str, list[str]] | None = None) -> MissionRow:
    return MissionRow(
        mission_id=r["mission_id"],
        path=r["path"],
        robot_id=r.get("robot_id"),
        start_time=str(r["start_time"]) if r.get("start_time") else None,
        duration_s=round(r.get("duration_s") or 0.0, 2),
        message_count=int(r.get("message_count") or 0),
        health_score=round(r.get("health_score") or 0.0, 1),
        verdict=r.get("verdict") or "",
        tags=(tags or {}).get(r["mission_id"], []),
    )


def _tag_map(cat: Catalog) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for r in cat.query("SELECT mission_id, tag FROM tags"):
        out.setdefault(r["mission_id"], []).append(r["tag"])
    return out


def register(mcp: Any) -> None:
    @mcp.tool(name="catalog.add_source")
    def add_source(path: str, pattern: str | None = None, background: bool = True,
                   with_signals: bool = True, force: bool = False) -> SourceAdded:
        """Register a directory of recordings and start indexing it.

        Indexing extracts metadata, per-topic health, detected events and a small set of
        numeric signals, so every later catalog.* and compare.* question is answered from
        the local database instead of by reopening bags. Poll catalog.index_status.

        Already-indexed missions are skipped unless `force=true`.
        """
        cat = catalog()
        root = resolve(path)
        files = discover(root, pattern)
        cat.add_source(str(root), pattern or "")
        known = cat.indexed_ids()
        already = sum(1 for f in files if cat.mission_by_path(str(f)))
        if background:
            index_in_background(files, cat, force=force, with_signals=with_signals)
        else:
            index_paths(files, cat, force=force, with_signals=with_signals)
        total = cat.query("SELECT COALESCE(SUM(duration_s),0) AS d FROM missions")[0]["d"]
        return SourceAdded(
            root=str(root),
            files_found=len(files),
            already_indexed=already,
            indexing_started=background,
            total_duration_s=round(total, 1),
            note=(
                f"{len(files)} recordings found; {len(known)} missions already in the catalog. "
                "Call catalog.index_status to watch progress."
            ),
        )

    @mcp.tool(name="catalog.index_status")
    def index_status() -> IndexProgress:
        """Indexing progress, so you can wait intelligently instead of failing.

        If `running` is true and `progress` is low, ask a different question first and
        come back — the answers get better as the corpus fills in.
        """
        # read through the module: the indexer rebinds its STATUS global on every run,
        # so a name imported once would always report the first, stale object
        status = _indexer.STATUS
        return IndexProgress(
            running=status.running,
            total=status.total,
            done=status.done,
            failed=status.failed,
            progress=round(status.progress, 3),
            elapsed_s=round(status.elapsed_s, 1),
            eta_s=round(status.eta_s, 1),
            current=Path(status.current).name if status.current else "",
            errors=status.errors,
        )

    @mcp.tool(name="catalog.list_missions")
    def list_missions(
        robot_id: str | None = None,
        tag: str | None = None,
        has_topic: str | None = None,
        min_duration_s: float | None = None,
        max_health_score: float | None = None,
        verdict: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> MissionList:
        """List indexed missions with filters. Paginated.

        Filter by robot, tag, presence of a topic, duration, health score or verdict.
        Use `max_health_score` to find the recordings worth investigating first.
        """
        cat = catalog()
        where: list[str] = []
        params: list[Any] = []
        if robot_id:
            where.append("m.robot_id = ?")
            params.append(robot_id)
        if verdict:
            where.append("m.verdict = ?")
            params.append(verdict)
        if min_duration_s is not None:
            where.append("m.duration_s >= ?")
            params.append(min_duration_s)
        if max_health_score is not None:
            where.append("m.health_score <= ?")
            params.append(max_health_score)
        if tag:
            where.append("EXISTS (SELECT 1 FROM tags t WHERE t.mission_id = m.mission_id AND t.tag = ?)")
            params.append(tag)
        if has_topic:
            where.append("EXISTS (SELECT 1 FROM topics tp WHERE tp.mission_id = m.mission_id AND tp.topic = ?)")
            params.append(has_topic)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        total = cat.query(f"SELECT COUNT(*) AS n FROM missions m{clause}", params)[0]["n"]
        rows = cat.query(
            f"SELECT * FROM missions m{clause} ORDER BY m.start_time DESC NULLS LAST "
            f"LIMIT {int(limit)} OFFSET {int(offset)}",
            params,
        )
        tags = _tag_map(cat)
        result = MissionList(
            missions=[_row(r, tags) for r in rows],
            total=int(total),
            provenance=Provenance(method="catalog_query", sample_count=int(total)),
        )

        def fewer(r: MissionList) -> MissionList:
            r.missions = r.missions[: max(5, len(r.missions) // 2)]
            return r

        return apply_budget(
            result, ladder=(fewer, fewer, fewer),
            narrowing=f"{total} missions match; page with offset, or add a filter",
            continuation={"offset": offset + limit, "limit": limit},
        )

    @mcp.tool(name="catalog.mission_info")
    def mission_info(mission_id: str | None = None, path: str | None = None) -> MissionInfo:
        """Everything the catalog knows about one mission: topics, health, events, signals.

        Answered from the index — no bag is reopened. Use health.audit_recording when you
        need the live detail rather than the cached summary.
        """
        cat = catalog()
        if path and not mission_id:
            row = cat.mission_by_path(str(resolve(path)))
        else:
            rows = cat.query("SELECT * FROM missions WHERE mission_id = ?", [mission_id])
            row = rows[0] if rows else None
        if row is None:
            return MissionInfo(caveats=["mission not found in the catalog — call catalog.add_source"])
        mid = row["mission_id"]
        meta = json.loads(row.get("metadata") or "{}")
        return MissionInfo(
            mission=_row(row, _tag_map(cat)),
            topics=cat.query(
                "SELECT topic, msg_type, count, expected_hz, actual_hz, jitter_cv, gap_count, "
                "estimated_dropped, score FROM topics WHERE mission_id = ? ORDER BY score", [mid]
            ),
            events=cat.query(
                "SELECT finding_id, kind, topic, t, t_end, severity, summary FROM events "
                "WHERE mission_id = ? ORDER BY severity DESC, t LIMIT 25", [mid]
            ),
            signals=[r["signal_key"] for r in cat.query(
                "SELECT signal_key FROM signals WHERE mission_id = ?", [mid])],
            caveats=meta.get("caveats", []),
            provenance=Provenance(mission_id=mid, path=row["path"], method="catalog_lookup"),
        )

    @mcp.tool(name="catalog.search_missions")
    def search_missions(query: str, limit: int = 25) -> MissionList:
        """Free-text search across log patterns, event summaries, topics, paths and tags.

        Use it when you remember what happened but not where: "battery", "timeout",
        "/camera/left", "customer:meridian".
        """
        cat = catalog()
        like = f"%{query.lower()}%"
        rows = cat.query(
            """SELECT DISTINCT m.* FROM missions m
               LEFT JOIN events e ON e.mission_id = m.mission_id
               LEFT JOIN topics t ON t.mission_id = m.mission_id
               LEFT JOIN tags g ON g.mission_id = m.mission_id
               LEFT JOIN log_patterns l ON l.mission_id = m.mission_id
               WHERE lower(m.path) LIKE ? OR lower(COALESCE(e.summary,'')) LIKE ?
                  OR lower(COALESCE(t.topic,'')) LIKE ? OR lower(COALESCE(g.tag,'')) LIKE ?
                  OR lower(COALESCE(l.template,'')) LIKE ?
               ORDER BY m.health_score LIMIT ?""",
            [like, like, like, like, like, int(limit)],
        )
        tags = _tag_map(cat)
        return MissionList(
            missions=[_row(r, tags) for r in rows],
            total=len(rows),
            provenance=Provenance(method=f"search({query})", sample_count=len(rows)),
        )

    @mcp.tool(name="catalog.tag_mission")
    def tag_mission(mission_id: str, tag: str, remove: bool = False) -> MissionInfo:
        """Attach a durable label to a mission — "regression", "customer:meridian", "verified".

        Tags persist across sessions, so this is how you leave notes for your future self
        or narrow a later cohort comparison.
        """
        cat = catalog()
        if remove:
            cat.remove_tag(mission_id, tag)
        else:
            cat.add_tag(mission_id, tag)
        return mission_info(mission_id=mission_id)

    @mcp.tool(name="catalog.fleet_summary")
    def fleet_summary(limit_worst: int = 5) -> FleetSummary:
        """Corpus-level statistics: mission count, health distribution, worst offenders.

        The right first call on an unfamiliar corpus — it tells you how much data exists,
        how healthy it is, and which recordings to look at first.
        """
        cat = catalog()
        agg = cat.query(
            """SELECT COUNT(*) AS n, COUNT(DISTINCT robot_id) AS robots,
                      COALESCE(SUM(duration_s),0) AS dur, COALESCE(SUM(size_bytes),0) AS size,
                      COALESCE(SUM(message_count),0) AS msgs, COALESCE(AVG(health_score),0) AS health
               FROM missions"""
        )[0]
        verdicts = {
            r["verdict"]: int(r["n"])
            for r in cat.query("SELECT verdict, COUNT(*) AS n FROM missions GROUP BY verdict")
        }
        worst = cat.query(
            "SELECT * FROM missions ORDER BY health_score LIMIT ?", [int(limit_worst)]
        )
        common = cat.query(
            """SELECT topic, COUNT(*) AS missions, AVG(actual_hz) AS mean_hz
               FROM topics GROUP BY topic ORDER BY missions DESC LIMIT 15"""
        )
        kinds = cat.query(
            """SELECT kind, COUNT(*) AS n, AVG(severity) AS mean_severity
               FROM events GROUP BY kind ORDER BY n DESC LIMIT 10"""
        )
        tags = _tag_map(cat)
        return FleetSummary(
            missions=int(agg["n"]),
            robots=int(agg["robots"] or 0),
            total_duration_h=round((agg["dur"] or 0) / 3600, 2),
            total_size_gb=round((agg["size"] or 0) / 1e9, 3),
            total_messages=int(agg["msgs"] or 0),
            verdicts=verdicts,
            mean_health=round(agg["health"] or 0, 1),
            worst_missions=[_row(r, tags) for r in worst],
            common_topics=[
                {"topic": r["topic"], "missions": int(r["missions"]),
                 "mean_hz": round(r["mean_hz"] or 0, 2)}
                for r in common
            ],
            top_event_kinds=[
                {"kind": r["kind"], "count": int(r["n"]),
                 "mean_severity": round(r["mean_severity"] or 0, 2)}
                for r in kinds
            ],
            provenance=Provenance(method="fleet_aggregate", sample_count=int(agg["n"])),
        )
