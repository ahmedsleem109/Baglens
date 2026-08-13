"""Reader protocol.

The interface the detectors need is deliberately small, and deliberately
**payload-free**: integrity analysis needs timing and structure only. Auditing a
50 GB bag reads timing records, not image data.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple, Protocol, runtime_checkable


class Arrival(NamedTuple):
    """One message's timing record. No payload, by design."""

    topic: str
    log_time_ns: int
    publish_time_ns: int
    size_bytes: int


@dataclass
class TopicInfo:
    topic: str
    msg_type: str = ""
    count: int = 0
    qos: dict[str, str] = field(default_factory=dict)
    #: deadline in seconds if the recorded QoS declares one
    declared_period_s: float | None = None


@dataclass
class BagMetadata:
    path: str
    format: str = "unknown"  # mcap | db3 | bag1 | ulog
    size_bytes: int = 0
    start_time_ns: int = 0
    end_time_ns: int = 0
    message_count: int = 0
    topics: list[TopicInfo] = field(default_factory=list)
    partial: bool = False
    in_progress: bool = False
    has_summary: bool = True
    warnings: list[str] = field(default_factory=list)

    @property
    def duration_s(self) -> float:
        return max(0.0, (self.end_time_ns - self.start_time_ns) / 1e9)

    def topic(self, name: str) -> TopicInfo | None:
        return next((t for t in self.topics if t.topic == name), None)


@runtime_checkable
class BagReader(Protocol):
    """Read-only view of one recording."""

    path: Path

    def metadata(self) -> BagMetadata: ...

    def arrivals(self, topics: list[str] | None = None) -> Iterator[Arrival]:
        """Single pass over timing records, ordered by log time. The hot path."""
        ...

    def numeric_field(self, topic: str, path: str) -> Iterator[tuple[int, float]]:
        """(log_time_ns, value) for a dotted field path, decoding only this topic."""
        ...

    def messages(
        self, topics: list[str] | None = None, start_s: float | None = None, end_s: float | None = None
    ) -> Iterator[tuple[str, int, object]]:
        """(topic, log_time_ns, decoded_message). Use sparingly — this decodes."""
        ...

    def schema_text(self, topic: str) -> str: ...

    def close(self) -> None: ...


def open_bag(path: str | Path) -> BagReader:
    """Dispatch on extension. Never raises on a damaged file — degrade and say so."""
    p = Path(path)
    suffix = p.suffix.lower()
    if p.is_dir():
        # rosbag2 directory: metadata.yaml + one or more .db3/.mcap
        inner = sorted([*p.glob("*.mcap"), *p.glob("*.db3")])
        if not inner:
            raise FileNotFoundError(f"no .mcap or .db3 inside rosbag2 directory {p}")
        return open_bag(inner[0])
    if suffix == ".mcap":
        from .mcap_reader import McapReader

        return McapReader(p)
    if suffix in (".db3", ".sqlite3"):
        from .db3_reader import Db3Reader

        return Db3Reader(p)
    if suffix == ".bag":
        from .ros1_reader import Ros1Reader

        return Ros1Reader(p)
    if suffix in (".ulg", ".ulog"):
        from .ulog_reader import UlogReader

        return UlogReader(p)
    raise ValueError(f"unsupported recording format: {p.name}")


def dotted_get(msg: object, path: str) -> float | None:
    """Resolve ``twist.linear.x`` or ``ranges[0]`` against a decoded message."""
    cur: object = msg
    for part in path.split("."):
        if not part:
            continue
        idx = None
        if part.endswith("]") and "[" in part:
            part, _, rest = part.partition("[")
            idx = int(rest.rstrip("]"))
        cur = getattr(cur, part, None)
        if cur is None:
            return None
        if idx is not None:
            try:
                cur = cur[idx]
            except (IndexError, TypeError):
                return None
    try:
        return float(cur)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
