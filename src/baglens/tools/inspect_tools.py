"""`inspect.*` — single-mission reads. Table stakes, kept cheap and hard-capped."""

from __future__ import annotations

from typing import Any

import numpy as np
from pydantic import BaseModel, Field

from ..budget import decimate, estimate_tokens
from ..config import CONFIG
from ..kernels.timeseries import describe
from ..provenance import Provenance, mission_id_for
from ..readers import open_bag
from ..readers.base import dotted_get
from .common import is_redacted_field, mask_payload, resolve


class TopicSummary(BaseModel):
    topic: str
    msg_type: str
    count: int
    hz: float
    first_s: float
    last_s: float
    declared_hz: float | None = None


class TopicList(BaseModel):
    path: str
    format: str
    duration_s: float
    message_count: int
    size_bytes: int
    partial: bool = False
    in_progress: bool = False
    topics: list[TopicSummary] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    provenance: Provenance = Field(default_factory=Provenance)


class SchemaText(BaseModel):
    topic: str
    msg_type: str
    definition: str
    provenance: Provenance = Field(default_factory=Provenance)


class MessageSample(BaseModel):
    topic: str
    t: float
    data: dict[str, Any]


class SampleSet(BaseModel):
    samples: list[MessageSample] = Field(default_factory=list)
    requested: int = 0
    available: int = 0
    truncated: bool = False
    suggested_narrowing: str | None = None
    provenance: Provenance = Field(default_factory=Provenance)


class FieldStats(BaseModel):
    topic: str
    field_path: str
    stats: dict[str, float] = Field(default_factory=dict)
    gaps_in_range: int = 0
    provenance: Provenance = Field(default_factory=Provenance)


def _to_dict(msg: Any, depth: int = 0) -> Any:
    if depth > 4:
        return "…"
    if isinstance(msg, (int, float, str, bool)) or msg is None:
        return msg
    if isinstance(msg, bytes):
        return f"<{len(msg)} bytes>"
    if isinstance(msg, (list, tuple)):
        head = [_to_dict(x, depth + 1) for x in msg[:8]]
        return head + ([f"… {len(msg) - 8} more"] if len(msg) > 8 else [])
    slots = getattr(msg, "__slots__", None) or getattr(msg, "__dict__", {})
    keys = list(slots) if not isinstance(slots, dict) else list(slots.keys())
    return {k: _to_dict(getattr(msg, k, None), depth + 1) for k in keys if not k.startswith("_")}


def register(mcp: Any) -> None:
    @mcp.tool(name="inspect.list_topics")
    def list_topics(path: str) -> TopicList:
        """Topics, types, message counts and observed rates for one recording.

        Cheap — reads the index, not the messages. Call this before any query so you
        know what exists; call health.audit_recording before trusting what it says.
        """
        p = resolve(path)
        reader = open_bag(p)
        meta = reader.metadata()
        dur = meta.duration_s or 1.0
        topics = [
            TopicSummary(
                topic=t.topic,
                msg_type=t.msg_type,
                count=t.count,
                hz=round(t.count / dur, 3),
                first_s=0.0,
                last_s=round(meta.duration_s, 3),
                declared_hz=round(1 / t.declared_period_s, 3) if t.declared_period_s else None,
            )
            for t in meta.topics
        ]
        reader.close()
        return TopicList(
            path=str(p),
            format=meta.format,
            duration_s=round(meta.duration_s, 3),
            message_count=meta.message_count,
            size_bytes=meta.size_bytes,
            partial=meta.partial,
            in_progress=meta.in_progress,
            topics=topics,
            warnings=meta.warnings,
            provenance=Provenance(
                path=str(p),
                topics=[t.topic for t in meta.topics],
                time_range=(0.0, meta.duration_s),
                method="metadata_index",
                sample_count=meta.message_count,
                partial=meta.partial,
            ),
        )

    @mcp.tool(name="inspect.topic_schema")
    def topic_schema(path: str, topic: str) -> SchemaText:
        """The message definition for a topic, so you know which field paths exist.

        Call this before timeseries.extract or inspect.field_stats rather than guessing
        a field path and getting an empty series back.
        """
        p = resolve(path)
        reader = open_bag(p)
        info = reader.metadata().topic(topic)
        text = reader.schema_text(topic)
        reader.close()
        return SchemaText(
            topic=topic,
            msg_type=info.msg_type if info else "",
            definition=text,
            provenance=Provenance(path=str(p), topics=[topic], method="schema_record"),
        )

    @mcp.tool(name="inspect.sample_messages")
    def sample_messages(
        path: str,
        topic: str,
        count: int = 5,
        start_s: float | None = None,
        end_s: float | None = None,
    ) -> SampleSet:
        """A few decoded messages, decimated across the range. For *shape*, not bulk.

        Hard-capped at 20. If you want values over time use timeseries.extract, which
        returns statistics instead of payloads and costs a fraction of the context.
        """
        p = resolve(path)
        count = max(1, min(count, 20))
        reader = open_bag(p)
        collected: list[tuple[str, int, Any]] = []
        for item in reader.messages([topic], start_s, end_s):
            collected.append(item)
            if len(collected) > 2000:
                break
        meta = reader.metadata()
        t0 = meta.start_time_ns
        chosen = decimate(collected, count)
        reader.close()
        return SampleSet(
            samples=[
                MessageSample(
                    topic=tp,
                    t=round((ts - t0) / 1e9, 4),
                    data=mask_payload(tp, _to_dict(msg)),
                )
                for tp, ts, msg in chosen
            ],
            requested=count,
            available=len(collected),
            truncated=len(collected) > count,
            suggested_narrowing=(
                "samples are decimated across the range; narrow start_s/end_s to see a "
                "specific moment"
            )
            if len(collected) > count
            else None,
            provenance=Provenance(
                path=str(p),
                topics=[topic],
                time_range=(start_s or 0.0, end_s or meta.duration_s),
                method=f"decimated_sample(n={count})",
                sample_count=len(collected),
            ),
        )

    @mcp.tool(name="inspect.get_message_at")
    def get_message_at(path: str, topic: str, t: float) -> SampleSet:
        """The single message nearest a timestamp. The drill-down endpoint.

        Use after an anomaly or gap has been located, to see exactly what was published.
        """
        p = resolve(path)
        reader = open_bag(p)
        meta = reader.metadata()
        window = 2.0
        best: tuple[float, Any] | None = None
        for _tp, ts, msg in reader.messages([topic], max(0.0, t - window), t + window):
            rel = (ts - meta.start_time_ns) / 1e9
            if best is None or abs(rel - t) < abs(best[0] - t):
                best = (rel, msg)
        reader.close()
        samples = (
            [MessageSample(topic=topic, t=round(best[0], 4),
                           data=mask_payload(topic, _to_dict(best[1])))]
            if best
            else []
        )
        return SampleSet(
            samples=samples,
            requested=1,
            available=len(samples),
            provenance=Provenance(
                path=str(p), topics=[topic], time_range=(t - window, t + window),
                method="nearest_message", sample_count=len(samples),
            ),
        )

    @mcp.tool(name="inspect.field_stats")
    def field_stats(
        path: str,
        topic: str,
        field_path: str,
        start_s: float | None = None,
        end_s: float | None = None,
    ) -> FieldStats:
        """min/max/mean/std/percentiles for one numeric field. Cheap and high-value.

        `field_path` is dotted, e.g. "twist.twist.linear.x" or "ranges[0]". Prefer this
        over sampling messages when you only need the distribution.
        """
        p = resolve(path)
        if is_redacted_field(topic, field_path):
            return FieldStats(
                topic=topic,
                field_path=field_path,
                provenance=Provenance(
                    path=str(p), topics=[topic], method="redacted",
                    warnings=[
                        f"{topic}.{field_path} is redacted by configuration; no values "
                        "were read and none can be returned"
                    ],
                ),
            )
        reader = open_bag(p)
        values: list[float] = []
        for _tp, _ts, msg in reader.messages([topic], start_s, end_s):
            v = dotted_get(msg, field_path)
            if v is not None:
                values.append(v)
        meta = reader.metadata()
        reader.close()
        return FieldStats(
            topic=topic,
            field_path=field_path,
            stats={k: round(v, 6) for k, v in describe(np.asarray(values)).items()},
            provenance=Provenance(
                path=str(p),
                mission_id=mission_id_for(p),
                topics=[topic],
                time_range=(start_s or 0.0, end_s or meta.duration_s),
                method=f"field_stats({field_path})",
                sample_count=len(values),
            ),
        )

    _ = (estimate_tokens, CONFIG)
