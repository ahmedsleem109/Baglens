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


class _Timed(SimpleNamespace):
    """A dataset's usable timestamps, plus how many were unusable."""

    values: Any
    unset: int


def _timed(stamps: Any) -> _Timed | None:
    """Drop timestamps of 0, which mean *unset* rather than "at time zero".

    PX4 writes 0 when a topic is published before its timestamp field is populated —
    typically one sample of `parameter_update` or `transponder_report` at the very start.
    In a log that counts from boot this is harmless, because 0 is close to the real
    start anyway. In a log that carries absolute UTC time it is not: two flights in the
    public corpus pair three such samples with an epoch of 1.79e15 µs, which put
    `start_time_ns` at 0 and made a 214s recording report a duration of 56 years. That
    figure is the denominator of `w_stall`, so it silently invalidates the score as well
    as the summary.
    """
    if stamps is None or len(stamps) == 0:
        return None
    usable = stamps[stamps != 0]
    unset = len(stamps) - len(usable)
    return _Timed(values=usable if len(usable) else None, unset=unset)


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
        unset = 0
        for dataset in log.data_list:
            stamps = _timed(dataset.data.get("timestamp"))
            if stamps is None:
                continue
            unset += stamps.unset
            if stamps.values is None:
                continue
            # ULog timestamps are microseconds
            starts.append(int(stamps.values[0]) * 1000)
            ends.append(int(stamps.values[-1]) * 1000)
            total += len(stamps.values)
            meta.topics.append(
                TopicInfo(
                    topic=self._topic_name(dataset),
                    msg_type=dataset.name,
                    count=len(stamps.values),
                )
            )
        meta.start_time_ns = min(starts) if starts else 0
        meta.end_time_ns = max(ends) if ends else 0
        meta.message_count = total
        if unset:
            meta.warnings.append(
                f"{unset} sample(s) carry an unset timestamp of 0 and are excluded from "
                f"the timeline; they are counted nowhere and cannot be placed in time"
            )
        meta.topics.sort(key=lambda t: t.topic)
        meta.warnings.append(
            "ULog records a single timestamp per sample — recorder lag (D6b) is not measurable"
        )
        self._meta = meta
        return meta

    def arrivals(
        self, topics: list[str] | None = None, start_time_ns: int | None = None
    ) -> Iterator[Arrival]:
        log = self._open()
        wanted = set(topics) if topics else None
        rows: list[Arrival] = []
        for dataset in log.data_list:
            name = self._topic_name(dataset)
            if wanted is not None and name not in wanted:
                continue
            stamps = _timed(dataset.data.get("timestamp"))
            if stamps is None or stamps.values is None:
                continue
            width = 8 * max(len(dataset.data) - 1, 1)
            rows.extend(
                Arrival(name, int(ts) * 1000, int(ts) * 1000, width)
                for ts in stamps.values
            )
        rows.sort(key=lambda a: a.log_time_ns)
        # a ULog is never tailed — pyulog parses the whole file — so the resume point is
        # honoured by filtering rather than by seeking
        yield from (a for a in rows if start_time_ns is None or a.log_time_ns >= start_time_ns)

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
