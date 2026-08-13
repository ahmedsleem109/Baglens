"""DuckDB access layer. Embedded, zero-config, and fast enough that fleet questions
are answered from SQL in milliseconds instead of by reopening two hundred bags."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import duckdb

from ..config import CONFIG

INDEX_VERSION = 2
_SCHEMA = (Path(__file__).parent / "schema.sql").read_text()


class Catalog:
    """Thread-safe-enough wrapper: DuckDB connections are not shared across threads,
    so each thread gets its own handle onto the same file."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else CONFIG.catalog_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_lock = threading.Lock()
        with self._init_lock:
            self.conn().execute(_SCHEMA)

    def conn(self) -> duckdb.DuckDBPyConnection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = duckdb.connect(str(self.path))
            self._local.conn = c
        return c

    # -- writes ------------------------------------------------------------

    def upsert_mission(self, row: dict[str, Any]) -> None:
        c = self.conn()
        c.execute("DELETE FROM missions WHERE mission_id = ?", [row["mission_id"]])
        c.execute(
            """INSERT INTO missions (mission_id, path, format, robot_id, start_time, end_time,
                   duration_s, message_count, size_bytes, health_score, verdict,
                   index_version, indexed_at, metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, now(), ?)""",
            [
                row["mission_id"], row["path"], row["format"], row.get("robot_id"),
                row.get("start_time"), row.get("end_time"), row.get("duration_s"),
                row.get("message_count"), row.get("size_bytes"), row.get("health_score"),
                row.get("verdict"), INDEX_VERSION, json.dumps(row.get("metadata", {})),
            ],
        )

    def replace_rows(self, table: str, mission_id: str, rows: list[dict[str, Any]]) -> None:
        c = self.conn()
        c.execute(f"DELETE FROM {table} WHERE mission_id = ?", [mission_id])
        if not rows:
            return
        cols = list(rows[0])
        placeholders = ",".join("?" * len(cols))
        c.executemany(
            f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders})",
            [[r[col] for col in cols] for r in rows],
        )

    def add_tag(self, mission_id: str, tag: str, source: str = "agent") -> None:
        self.conn().execute(
            "INSERT OR REPLACE INTO tags VALUES (?, ?, ?, now())", [mission_id, tag, source]
        )

    def remove_tag(self, mission_id: str, tag: str) -> None:
        self.conn().execute(
            "DELETE FROM tags WHERE mission_id = ? AND tag = ?", [mission_id, tag]
        )

    def add_source(self, root: str, pattern: str) -> None:
        self.conn().execute(
            "INSERT OR REPLACE INTO sources VALUES (?, ?, now())", [root, pattern]
        )

    # -- reads -------------------------------------------------------------

    def query(self, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
        cur = self.conn().execute(sql, params or [])
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row, strict=False)) for row in cur.fetchall()]

    def indexed_ids(self) -> dict[str, int]:
        return {
            r["mission_id"]: r["index_version"]
            for r in self.query("SELECT mission_id, index_version FROM missions")
        }

    def mission_by_path(self, path: str) -> dict[str, Any] | None:
        rows = self.query("SELECT * FROM missions WHERE path = ?", [str(path)])
        return rows[0] if rows else None

    def count(self, table: str = "missions") -> int:
        return int(self.query(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"])
