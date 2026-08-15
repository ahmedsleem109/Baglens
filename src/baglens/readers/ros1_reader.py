"""ROS 1 `.bag` reader, via `rosbags` — no ROS installation required (G9).

ROS 1 bags carry a single timestamp per message, so recorder lag (D6b) is not
measurable and the ClockReport says so rather than inventing a flat curve.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .base import Arrival, BagMetadata, TopicInfo, dotted_get


class Ros1Reader:
    format = "bag1"

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._meta: BagMetadata | None = None

    def _reader(self) -> Any:
        from rosbags.highlevel import AnyReader

        return AnyReader([self.path])

    def metadata(self) -> BagMetadata:
        if self._meta is not None:
            return self._meta
        meta = BagMetadata(path=str(self.path), format="bag1", size_bytes=self.path.stat().st_size)
        with self._reader() as reader:
            meta.start_time_ns = int(reader.start_time)
            meta.end_time_ns = int(reader.end_time)
            meta.message_count = int(reader.message_count)
            for conn in reader.connections:
                existing = meta.topic(conn.topic)
                if existing is not None:
                    existing.count += conn.msgcount
                    continue
                meta.topics.append(
                    TopicInfo(topic=conn.topic, msg_type=conn.msgtype, count=conn.msgcount)
                )
        meta.topics.sort(key=lambda t: t.topic)
        self._meta = meta
        return meta

    def arrivals(
        self, topics: list[str] | None = None, start_time_ns: int | None = None
    ) -> Iterator[Arrival]:
        with self._reader() as reader:
            conns = [c for c in reader.connections if not topics or c.topic in topics]
            for conn, timestamp, raw in reader.messages(connections=conns, start=start_time_ns):
                yield Arrival(conn.topic, int(timestamp), int(timestamp), len(raw))

    def messages(
        self,
        topics: list[str] | None = None,
        start_s: float | None = None,
        end_s: float | None = None,
    ) -> Iterator[tuple[str, int, Any]]:
        meta = self.metadata()
        t0 = meta.start_time_ns
        with self._reader() as reader:
            conns = [c for c in reader.connections if not topics or c.topic in topics]
            start = int(t0 + start_s * 1e9) if start_s is not None else None
            end = int(t0 + end_s * 1e9) if end_s is not None else None
            for conn, timestamp, raw in reader.messages(connections=conns, start=start, stop=end):
                try:
                    yield conn.topic, int(timestamp), reader.deserialize(raw, conn.msgtype)
                except Exception:
                    continue

    def numeric_field(self, topic: str, path: str) -> Iterator[tuple[int, float]]:
        for _tp, t_ns, msg in self.messages([topic]):
            v = dotted_get(msg, path)
            if v is not None:
                yield t_ns, v

    def schema_text(self, topic: str) -> str:
        with self._reader() as reader:
            for conn in reader.connections:
                if conn.topic == topic:
                    return getattr(conn, "msgdef", "") or conn.msgtype
        return ""

    def close(self) -> None:
        return
