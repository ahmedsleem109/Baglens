"""G6 — provenance. Every claim traceable to (bag, topic, time range, method).

An LLM saying "the obstacle-avoidance latency regressed" is worthless without
"says who?". This model is the answer, and `export.report` renders it as footnotes.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from pydantic import BaseModel, Field


class Provenance(BaseModel):
    """Where a claim came from."""

    mission_id: str = ""
    path: str = ""
    topics: list[str] = Field(default_factory=list)
    time_range: tuple[float, float] = (0.0, 0.0)
    method: str = ""  # e.g. "gap_detector(k=5.0,floor=0.25s)"
    sample_count: int = 0
    partial: bool = False
    warnings: list[str] = Field(default_factory=list)

    def cite(self) -> str:
        """One-line human-readable citation."""
        topics = ",".join(self.topics[:3]) + ("…" if len(self.topics) > 3 else "")
        t0, t1 = self.time_range
        return f"{Path(self.path).name}:{topics}@[{t0:.2f},{t1:.2f}]s via {self.method}"


def mission_id_for(path: str | Path, sample_bytes: int = 1 << 20) -> str:
    """Content-derived id, stable across moves and renames.

    Hashes size + head + tail rather than the whole file — a 50 GB bag must not
    cost a full read just to be identified.
    """
    p = Path(path)
    h = hashlib.blake2b(digest_size=12)
    size = p.stat().st_size
    h.update(str(size).encode())
    with p.open("rb") as f:
        h.update(f.read(sample_bytes))
        if size > sample_bytes * 2:
            f.seek(-sample_bytes, os.SEEK_END)
            h.update(f.read(sample_bytes))
    return h.hexdigest()
