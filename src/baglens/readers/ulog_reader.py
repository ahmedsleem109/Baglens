"""PX4 ULog reader.

ULog is a flat table format: each logged struct is a "dataset" with a timestamp column
and numeric fields. That maps cleanly onto the arrival stream (one dataset = one topic),
which means every detector in the library works on PX4 flight logs unchanged — and PX4
publishes thousands of real flights with real failures, which is rare and valuable.

Requires `pyulog`; install with the `ulog` extra.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .base import Arrival, BagMetadata, TopicInfo, dotted_get


class UlogReader:
    format = "ulog"

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._meta: BagMetadata | None = None
        self._log: Any = None

    def _open(self) -> Any:
        if self._log is None:
            try:
                from pyulog import ULog
            except ImportError as exc:  # pragma: no cover - optional dependency
                raise ImportError(
                    "reading PX4 .ulg requires pyulog — install baglens[ulog]"
                ) from exc
            self._log = ULog(str(self.path))
        return self._log

    @staticmethod
    def _topic_name(dataset: Any) -> str:
        multi = getattr(dataset, "multi_id", 0)
        return f"/{dataset.name}" + (f"_{multi}" if multi else "")

    def metadata(self) -> BagMetadata:
        if self._meta is not None:
            return self._meta
        log = self._open()
        meta = BagMetadata(path=str(self.path), format="ulog",
                           size_bytes=self.path.stat().st_size)
        starts: list[int] = []
        ends: list[int] = []
        total = 0
        for dataset in log.data_list:
            stamps = dataset.data.get("timestamp")
            if stamps is None or len(stamps) == 0:
                continue
            # ULog timestamps are microseconds since boot
            starts.append(int(stamps[0]) * 1000)
            ends.append(int(stamps[-1]) * 1000)
            total += len(stamps)
            meta.topics.append(
                TopicInfo(
                    topic=self._topic_name(dataset),
                    msg_type=dataset.name,
                    count=len(stamps),
                )
            )
        meta.start_time_ns = min(starts) if starts else 0
        meta.end_time_ns = max(ends) if ends else 0
        meta.message_count = total
        meta.topics.sort(key=lambda t: t.topic)
        meta.warnings.append(
            "ULog records a single timestamp per sample — recorder lag (D6b) is not measurable"
        )
        self._meta = meta
        return meta

    def arrivals(self, topics: list[str] | None = None) -> Iterator[Arrival]:
        log = self._open()
        wanted = set(topics) if topics else None
        rows: list[Arrival] = []
        for dataset in log.data_list:
            name = self._topic_name(dataset)
            if wanted is not None and name not in wanted:
                continue
            stamps = dataset.data.get("timestamp")
            if stamps is None:
                continue
            width = 8 * max(len(dataset.data) - 1, 1)
            rows.extend(
                Arrival(name, int(ts) * 1000, int(ts) * 1000, width) for ts in stamps
            )
        rows.sort(key=lambda a: a.log_time_ns)
        yield from rows

    def messages(
        self,
        topics: list[str] | None = None,
        start_s: float | None = None,
        end_s: float | None = None,
    ) -> Iterator[tuple[str, int, Any]]:
        log = self._open()
        meta = self.metadata()
        t0 = meta.start_time_ns
        wanted = set(topics) if topics else None
        for dataset in log.data_list:
            name = self._topic_name(dataset)
            if wanted is not None and name not in wanted:
                continue
            stamps = dataset.data.get("timestamp")
            if stamps is None:
                continue
            fields = [k for k in dataset.data if k != "timestamp"]
            for i, ts in enumerate(stamps):
                t_ns = int(ts) * 1000
                rel = (t_ns - t0) / 1e9
                if start_s is not None and rel < start_s:
                    continue
                if end_s is not None and rel > end_s:
                    break
                yield name, t_ns, SimpleNamespace(
                    **{f.replace("[", "_").replace("]", ""): float(dataset.data[f][i])
                       for f in fields}
                )

    def numeric_field(self, topic: str, path: str) -> Iterator[tuple[int, float]]:
        for _tp, t_ns, msg in self.messages([topic]):
            v = dotted_get(msg, path)
            if v is not None:
                yield t_ns, v

    def schema_text(self, topic: str) -> str:
        log = self._open()
        for dataset in log.data_list:
            if self._topic_name(dataset) == topic:
                return "\n".join(sorted(dataset.data))
        return ""

    def close(self) -> None:
        self._log = None
