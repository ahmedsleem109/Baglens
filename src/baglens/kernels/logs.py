"""Log text handling: Drain-style template extraction.

40,000 log lines become ~30 patterns with counts. This is the single largest context
saving in the whole tool surface — an agent that reads raw `/rosout` dies at line 400,
and the patterns carry almost all of the information anyway.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

LEVELS = {10: "DEBUG", 20: "INFO", 30: "WARN", 40: "ERROR", 50: "FATAL"}

#: token-level replacements applied before templating
_NUMBER = re.compile(r"(?<![\w.])[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?(?![\w.])")
_HEX = re.compile(r"\b0x[0-9a-fA-F]+\b")
_UUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_PATH = re.compile(r"(/[\w.\-]+){2,}")
_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")


@dataclass
class LogEntry:
    t: float
    level: str
    name: str
    msg: str


@dataclass
class Cluster:
    template: str
    level: str
    count: int = 0
    example: str = ""
    first_t: float = 0.0
    last_t: float = 0.0
    times: list[float] = field(default_factory=list)


def normalise(msg: str) -> str:
    s = _UUID.sub("<uuid>", msg)
    s = _HEX.sub("<hex>", s)
    s = _PATH.sub("<path>", s)
    s = _QUOTED.sub("<str>", s)
    s = _NUMBER.sub("<num>", s)
    return re.sub(r"\s+", " ", s).strip()


def cluster_templates(entries: list[LogEntry], max_clusters: int = 200) -> list[Cluster]:
    """Group by (level, normalised text). Bounded: rare templates are folded into `<other>`."""
    clusters: dict[tuple[str, str], Cluster] = {}
    for e in entries:
        key = (e.level, normalise(e.msg))
        c = clusters.get(key)
        if c is None:
            if len(clusters) >= max_clusters:
                key = (e.level, "<other>")
                c = clusters.get(key)
            if c is None:
                c = Cluster(template=key[1], level=key[0], example=e.msg, first_t=e.t)
                clusters[key] = c
        c.count += 1
        c.last_t = e.t
        if len(c.times) < 500:
            c.times.append(e.t)
    return sorted(clusters.values(), key=lambda c: -c.count)


def read_log_messages(path: str | Path, topics: list[str] | None = None) -> list[LogEntry]:
    """Decode log-type topics into (t, level, node, message)."""
    from ..readers import open_bag

    reader = open_bag(path)
    meta = reader.metadata()
    if topics is None:
        topics = [
            t.topic
            for t in meta.topics
            if t.msg_type in ("rcl_interfaces/msg/Log", "rosgraph_msgs/Log")
        ]
    if not topics:
        reader.close()
        return []

    t0 = meta.start_time_ns
    out: list[LogEntry] = []
    for _tp, ts, msg in reader.messages(topics):
        level_raw = getattr(msg, "level", 20)
        try:
            level = LEVELS.get(int(level_raw), str(level_raw))
        except (TypeError, ValueError):
            level = str(level_raw)
        out.append(
            LogEntry(
                t=(ts - t0) / 1e9,
                level=level,
                name=str(getattr(msg, "name", "")),
                msg=str(getattr(msg, "msg", "")),
            )
        )
    reader.close()
    return out


def read_diagnostics(path: str | Path) -> list[dict[str, Any]]:
    """Flatten `/diagnostics` into rows an agent can reason about."""
    from ..readers import open_bag

    reader = open_bag(path)
    meta = reader.metadata()
    topics = [t.topic for t in meta.topics if t.msg_type == "diagnostic_msgs/msg/DiagnosticArray"]
    if not topics:
        reader.close()
        return []
    t0 = meta.start_time_ns
    rows: list[dict[str, Any]] = []
    for _tp, ts, msg in reader.messages(topics):
        for status in getattr(msg, "status", []) or []:
            rows.append(
                {
                    "t": (ts - t0) / 1e9,
                    "level": int(getattr(status, "level", 0)),
                    "name": str(getattr(status, "name", "")),
                    "message": str(getattr(status, "message", "")),
                    "hardware_id": str(getattr(status, "hardware_id", "")),
                }
            )
    reader.close()
    return rows
