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
from .stamp_peek import peek_frame_id, peek_stamp_ns

#: message types whose serialised form begins with sec:int32, nanosec:uint32
_HEADERISH = ("std_msgs/msg/Header", "builtin_interfaces/msg/Time")


def _stamp_offset_for(msg_type: str, store: Any) -> int | None:
    """Where `sec` sits in this type's messages, read from the typestore's field list.

    `.db3` carries no message-definition text — `schema_text` returns a repr of the
    typestore's AST — so the text parser used for MCAP cannot be applied here. Asking the
    typestore directly is both correct and cheaper, and doing it any other way meant
    `stamp_offset` quietly returned None for every `.db3` topic, so F1 and F3 read nothing
    at all from this format while appearing to work.
    """
    if store is None or not msg_type:
        return None
    try:
        _constants, fields = store.fielddefs[msg_type]
    except Exception:
        return None
    if not fields:
        return None
    fname, ftype = fields[0][0], fields[0][1]
    # a NAME node carries the referenced type as its second element
    referenced = ftype[1] if isinstance(ftype, tuple) and len(ftype) > 1 else None
    if not isinstance(referenced, str):
        return None
    if referenced in _HEADERISH and fname in ("header", "stamp"):
        return 4
    return None


class Db3Reader:
    format = "db3"

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._meta: BagMetadata | None = None
        self._conn: sqlite3.Connection | None = None
        # These must be real attributes, not just `getattr` defaults: the auditor opts a
        # reader in with `if hasattr(reader, "want_stamps")`, so a reader that only reads
        # the flag but never declares it is silently never asked. That cost F3 the entire
        # transform tree of the one real recording that has a good one.
        self.want_stamps = False
        self.decode_topics: set[str] = set()
        self.frame_samples = 200
        self._store: Any = None

    def _typestore(self) -> Any:
        if self._store is None:
            from rosbags.typesys import Stores, get_typestore

            self._store = get_typestore(Stores.ROS2_HUMBLE)
        return self._store

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
        stamps = self.want_stamps
        decode = self.decode_topics
        # the blob is only pulled out of SQLite when someone asked for stamps or for a
        # decoded topic; the payload-free path stays payload-free
        need_data = stamps or bool(decode)
        cols = "t.name, m.timestamp, LENGTH(m.data)" + (", m.data" if need_data else "")
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
        if not need_data:
            for name, ts, size in db.execute(sql, params):
                yield Arrival(name, int(ts), int(ts), int(size or 0))
            return

        offsets: dict[str, int | None] = {}
        frames_read: dict[str, int] = {}
        frame_samples = self.frame_samples
        types = {t.topic: t.msg_type for t in self.metadata().topics}
        store = self._typestore()
        for name, ts, size, data in db.execute(sql, params):
            off = offsets.get(name, False)
            if off is False:
                off = _stamp_offset_for(types.get(name, ""), store)
                offsets[name] = off

            stamp = frame = None
            if stamps and off is not None and data:
                blob = bytes(data)
                stamp = peek_stamp_ns(blob, off)
                seen = frames_read.get(name, 0)
                if seen < frame_samples:
                    frames_read[name] = seen + 1
                    frame = peek_frame_id(blob, off)

            decoded = None
            if name in decode and data:
                # the same opt-in as the MCAP path: `/tf` is a bare sequence with no
                # top-level header, so F3 cannot peek at it and has to decode
                try:
                    decoded = store.deserialize_cdr(bytes(data), types.get(name, ""))
                except Exception:
                    decoded = None

            yield Arrival(name, int(ts), int(ts), int(size or 0), stamp, frame, decoded)

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
