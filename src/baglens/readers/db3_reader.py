"""rosbag2 SQLite (.db3) reader.

Timing comes straight out of the `messages` table, so the arrival stream is a single
indexed SELECT and never touches a blob. `.db3` records only one timestamp per message,
so publish_time is reported equal to log_time and D6b (recorder lag) is not available —
the ClockReport says so rather than inventing a curve.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .base import Arrival, BagMetadata, TopicInfo, dotted_get
from .stamp_peek import peek_stamp_ns, stamp_offset


class Db3Reader:
    format = "db3"

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._meta: BagMetadata | None = None
        self._conn: sqlite3.Connection | None = None

    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        return self._conn

    def _sidecar(self) -> dict[str, Any]:
        yml = self.path.parent / "metadata.yaml"
        if not yml.exists():
            return {}
        try:
            import yaml

            return yaml.safe_load(yml.read_text()) or {}
        except Exception:
            return {}

    def metadata(self) -> BagMetadata:
        if self._meta is not None:
            return self._meta
        meta = BagMetadata(path=str(self.path), format="db3", size_bytes=self.path.stat().st_size)
        db = self._db()
        side = self._sidecar()
        if not side:
            meta.warnings.append("no metadata.yaml — topic list reconstructed from SQLite schema")

        qos_by_topic: dict[str, str] = {}
        info = side.get("rosbag2_bagfile_information", {}) if isinstance(side, dict) else {}
        for entry in info.get("topics_with_message_count", []) or []:
            tmeta = entry.get("topic_metadata", {})
            if tmeta.get("name"):
                qos_by_topic[tmeta["name"]] = tmeta.get("offered_qos_profiles", "")

        rows = db.execute(
            "SELECT t.name, t.type, COUNT(m.id), MIN(m.timestamp), MAX(m.timestamp) "
            "FROM topics t LEFT JOIN messages m ON m.topic_id = t.id GROUP BY t.id"
        ).fetchall()
        starts, ends, total = [], [], 0
        for name, mtype, count, tmin, tmax in rows:
            total += count or 0
            if tmin:
                starts.append(tmin)
                ends.append(tmax)
            from .mcap_reader import _qos_period

            qos_raw = {"offered_qos_profiles": qos_by_topic.get(name, "")}
            meta.topics.append(
                TopicInfo(
                    topic=name,
                    msg_type=mtype or "",
                    count=count or 0,
                    qos=qos_raw,
                    declared_period_s=_qos_period(qos_raw),
                )
            )
        meta.start_time_ns = min(starts) if starts else 0
        meta.end_time_ns = max(ends) if ends else 0
        meta.message_count = total
        meta.topics.sort(key=lambda t: t.topic)
        self._meta = meta
        return meta

    def arrivals(
        self, topics: list[str] | None = None, start_time_ns: int | None = None
    ) -> Iterator[Arrival]:
        db = self._db()
        stamps = getattr(self, "want_stamps", False)
        # the blob is only pulled out of SQLite when someone asked for stamps; the
        # payload-free path stays payload-free
        cols = "t.name, m.timestamp, LENGTH(m.data)" + (", m.data" if stamps else "")
        sql = f"SELECT {cols} FROM messages m JOIN topics t ON t.id = m.topic_id"
        params: list[Any] = []
        where: list[str] = []
        if topics:
            where.append(f"t.name IN ({','.join('?' * len(topics))})")
            params = list(topics)
        if start_time_ns is not None:
            # a resuming reader pays for the rows it has not seen, not the whole table
            where.append("m.timestamp >= ?")
            params.append(int(start_time_ns))
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY m.timestamp"
        if not stamps:
            for name, ts, size in db.execute(sql, params):
                yield Arrival(name, int(ts), int(ts), int(size or 0))
            return

        offsets: dict[str, int | None] = {}
        for name, ts, size, data in db.execute(sql, params):
            off = offsets.get(name, False)
            if off is False:
                off = stamp_offset(self.schema_text(name))
                offsets[name] = off
            stamp = peek_stamp_ns(bytes(data), off) if off is not None and data else None
            yield Arrival(name, int(ts), int(ts), int(size or 0), stamp)

    def messages(
        self,
        topics: list[str] | None = None,
        start_s: float | None = None,
        end_s: float | None = None,
    ) -> Iterator[tuple[str, int, Any]]:
        from rosbags.typesys import Stores, get_typestore

        store = get_typestore(Stores.LATEST)
        meta = self.metadata()
        t0 = meta.start_time_ns
        db = self._db()
        sql = (
            "SELECT t.name, t.type, m.timestamp, m.data FROM messages m "
            "JOIN topics t ON t.id = m.topic_id"
        )
        clauses: list[str] = []
        params: list[Any] = []
        if topics:
            clauses.append(f"t.name IN ({','.join('?' * len(topics))})")
            params += list(topics)
        if start_s is not None:
            clauses.append("m.timestamp >= ?")
            params.append(int(t0 + start_s * 1e9))
        if end_s is not None:
            clauses.append("m.timestamp <= ?")
            params.append(int(t0 + end_s * 1e9))
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY m.timestamp"
        for name, mtype, ts, data in db.execute(sql, params):
            try:
                yield name, int(ts), store.deserialize_cdr(data, mtype)
            except Exception:
                continue

    def numeric_field(self, topic: str, path: str) -> Iterator[tuple[int, float]]:
        for _t, t_ns, msg in self.messages(topics=[topic]):
            v = dotted_get(msg, path)
            if v is not None:
                yield t_ns, v

    def schema_text(self, topic: str) -> str:
        info = self.metadata().topic(topic)
        if info is None:
            return ""
        try:
            from rosbags.typesys import Stores, get_typestore

            store = get_typestore(Stores.LATEST)
            return str(store.fielddefs.get(info.msg_type, ""))
        except Exception:
            return info.msg_type

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
