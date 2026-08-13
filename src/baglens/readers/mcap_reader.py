"""MCAP reader with recovery (G4).

Two paths:
  * summary present  → indexed reads, O(1) seeks, exact statistics for free.
  * summary absent   → sequential chunk scan, ``partial=True``, report the last
    readable timestamp. This is the file that is still being written, or whose
    recorder crashed. Existing tools simply fail here. Do not fail — degrade and say so.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from mcap.reader import make_reader
from mcap.records import Channel, Message, Schema
from mcap.stream_reader import StreamReader

from .base import Arrival, BagMetadata, TopicInfo, dotted_get


def recover_messages(path: Path) -> Iterator[tuple[Schema | None, Channel, Message, int]]:
    """Sequential record scan for files with no summary section.

    Built on ``StreamReader`` rather than ``NonSeekingReader``: the latter gives up on
    a file whose tail is missing, which is precisely the file we are trying to rescue.
    Yields ``(schema, channel, message, offset)`` and simply stops when the records run
    out mid-stream — the caller reports how far it got.
    """
    schemas: dict[int, Schema] = {}
    channels: dict[int, Channel] = {}
    with path.open("rb") as f:
        try:
            for record in StreamReader(f, skip_magic=False).records:
                if isinstance(record, Schema):
                    schemas[record.id] = record
                elif isinstance(record, Channel):
                    channels[record.id] = record
                elif isinstance(record, Message):
                    channel = channels.get(record.channel_id)
                    if channel is not None:
                        yield schemas.get(channel.schema_id), channel, record, f.tell()
        except Exception:
            return


def _qos_period(channel_metadata: dict[str, str]) -> float | None:
    """Extract a deadline from recorded ROS 2 QoS, if declared."""
    raw = channel_metadata.get("offered_qos_profiles") or channel_metadata.get("qos")
    if not raw:
        return None
    try:
        import yaml

        profiles = yaml.safe_load(raw)
    except Exception:
        return None
    if isinstance(profiles, dict):
        profiles = [profiles]
    if not isinstance(profiles, list):
        return None
    for prof in profiles:
        if not isinstance(prof, dict):
            continue
        dl = prof.get("deadline")
        if isinstance(dl, dict):
            sec = float(dl.get("sec") or 0) + float(dl.get("nsec") or dl.get("nanosec") or 0) / 1e9
            # rmw uses a huge sentinel for "no deadline"
            if 0.0 < sec < 1e6:
                return sec
    return None


class McapReader:
    format = "mcap"

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._meta: BagMetadata | None = None
        self._decoder_factories: list[Any] | None = None

    # -- internals ---------------------------------------------------------

    def _factories(self) -> list[Any]:
        if self._decoder_factories is None:
            try:
                from mcap_ros2.decoder import DecoderFactory

                self._decoder_factories = [DecoderFactory()]
            except Exception:
                self._decoder_factories = []
        return self._decoder_factories

    def _is_growing(self) -> bool:
        st = self.path.stat()
        return (time.time() - st.st_mtime) < 5.0

    # -- BagReader ---------------------------------------------------------

    def metadata(self) -> BagMetadata:
        if self._meta is not None:
            return self._meta
        meta = BagMetadata(path=str(self.path), format="mcap", size_bytes=self.path.stat().st_size)
        summary = None
        try:
            with self.path.open("rb") as f:
                summary = make_reader(f).get_summary()
        except Exception as exc:
            meta.warnings.append(f"summary read failed: {exc}")

        if summary is not None and summary.statistics is not None:
            meta.has_summary = True
            st = summary.statistics
            meta.start_time_ns = st.message_start_time
            meta.end_time_ns = st.message_end_time
            meta.message_count = st.message_count
            for cid, ch in summary.channels.items():
                schema = summary.schemas.get(ch.schema_id)
                meta.topics.append(
                    TopicInfo(
                        topic=ch.topic,
                        msg_type=schema.name if schema else "",
                        count=st.channel_message_counts.get(cid, 0),
                        qos=dict(ch.metadata),
                        declared_period_s=_qos_period(ch.metadata),
                    )
                )
        else:
            meta.has_summary = False
            meta.partial = True
            meta.warnings.append(
                "no MCAP summary section — recovered by sequential scan "
                "(file in progress or recorder crashed)"
            )
            self._scan_metadata(meta)

        meta.in_progress = meta.partial and self._is_growing()
        meta.topics.sort(key=lambda t: t.topic)
        self._meta = meta
        return meta

    def _scan_metadata(self, meta: BagMetadata) -> None:
        """Recovery path: one sequential pass to rebuild what the summary would have said."""
        counts: dict[str, int] = {}
        types: dict[str, str] = {}
        qos: dict[str, dict[str, str]] = {}
        first = last = 0
        n = 0
        for schema, channel, message, _offset in recover_messages(self.path):
            n += 1
            if first == 0:
                first = message.log_time
            last = max(last, message.log_time)
            counts[channel.topic] = counts.get(channel.topic, 0) + 1
            if channel.topic not in types:
                types[channel.topic] = schema.name if schema else ""
                qos[channel.topic] = dict(channel.metadata)
        if n:
            meta.warnings.append(f"recovered {n} messages by sequential scan")
        meta.start_time_ns, meta.end_time_ns, meta.message_count = first, last, n
        meta.topics = [
            TopicInfo(
                topic=t,
                msg_type=types.get(t, ""),
                count=c,
                qos=qos.get(t, {}),
                declared_period_s=_qos_period(qos.get(t, {})),
            )
            for t, c in counts.items()
        ]

    def arrivals(self, topics: list[str] | None = None) -> Iterator[Arrival]:
        meta = self.metadata()
        wanted = set(topics) if topics else None

        if not meta.has_summary:
            # recovery path: records in file order, which is log order for any recorder
            for _schema, channel, message, _off in recover_messages(self.path):
                if wanted is None or channel.topic in wanted:
                    yield Arrival(
                        channel.topic,
                        message.log_time,
                        message.publish_time or message.log_time,
                        len(message.data),
                    )
            return

        with self.path.open("rb") as f:
            it = make_reader(f).iter_messages(topics=topics, log_time_order=True)
            try:
                for _schema, channel, message in it:
                    yield Arrival(
                        channel.topic,
                        message.log_time,
                        message.publish_time or message.log_time,
                        len(message.data),
                    )
            except GeneratorExit:
                raise
            except Exception:
                # truncated tail: yield what we have, the caller reports partial
                return

    def messages(
        self,
        topics: list[str] | None = None,
        start_s: float | None = None,
        end_s: float | None = None,
    ) -> Iterator[tuple[str, int, Any]]:
        meta = self.metadata()
        t0 = meta.start_time_ns
        start_ns = int(t0 + start_s * 1e9) if start_s is not None else None
        end_ns = int(t0 + end_s * 1e9) if end_s is not None else None
        with self.path.open("rb") as f:
            reader = make_reader(f, decoder_factories=self._factories())
            try:
                for _s, channel, message, decoded in reader.iter_decoded_messages(
                    topics=topics, start_time=start_ns, end_time=end_ns, log_time_order=True
                ):
                    yield channel.topic, message.log_time, decoded
            except Exception:
                return

    def numeric_field(self, topic: str, path: str) -> Iterator[tuple[int, float]]:
        for _topic, t_ns, msg in self.messages(topics=[topic]):
            v = dotted_get(msg, path)
            if v is not None:
                yield t_ns, v

    def schema_text(self, topic: str) -> str:
        try:
            with self.path.open("rb") as f:
                summary = make_reader(f).get_summary()
                if summary is None:
                    return ""
                for ch in summary.channels.values():
                    if ch.topic == topic:
                        schema = summary.schemas.get(ch.schema_id)
                        if schema is None:
                            return ""
                        return schema.data.decode("utf-8", "replace")
        except Exception:
            pass
        return ""

    def close(self) -> None:
        return
