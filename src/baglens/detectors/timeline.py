"""Topic density timeline — one row per topic, one column per time bucket.

A compact text density map costs ~300 tokens and lets a model see the *shape* of a
failure instantly: which topics were alive when, and whether they died together.

Streaming constraint: the end time is unknown, so the bucket width cannot be chosen
up front. Start narrow and halve the resolution whenever the row overflows — the
column count stays bounded and the width is reported alongside.
"""

from __future__ import annotations

from ..models import Timeline
from ..provenance import Provenance

DENSITY_CHARS = " ·░▒▓█"


class TimelineAccumulator:
    """Bounded density map. State: ``max_cols`` ints per topic."""

    __slots__ = ("max_cols", "bucket_s", "rows", "t_end")

    def __init__(self, max_cols: int = 120, bucket_s: float = 0.5) -> None:
        self.max_cols = max_cols
        self.bucket_s = bucket_s
        self.rows: dict[str, list[int]] = {}
        self.t_end = 0.0

    def push(self, topic: str, t: float) -> None:
        self.t_end = max(self.t_end, t)
        idx = int(t / self.bucket_s)
        while idx >= self.max_cols:
            self._coarsen()
            idx = int(t / self.bucket_s)
        row = self.rows.get(topic)
        if row is None:
            row = [0] * self.max_cols
            self.rows[topic] = row
        row[idx] += 1

    def _coarsen(self) -> None:
        self.bucket_s *= 2
        for topic, row in self.rows.items():
            merged = [row[i] + (row[i + 1] if i + 1 < len(row) else 0) for i in range(0, len(row), 2)]
            merged += [0] * (self.max_cols - len(merged))
            self.rows[topic] = merged

    def render(self, prov: Provenance | None = None, width: int | None = None) -> Timeline:
        cols = max(1, int(self.t_end / self.bucket_s) + 1)
        cols = min(cols, self.max_cols)
        if width and width < cols:
            step = cols / width
            sample = [int(i * step) for i in range(width)]
        else:
            sample = list(range(cols))

        name_w = max((len(t) for t in self.rows), default=8)
        lines: list[str] = []
        for topic in sorted(self.rows):
            row = self.rows[topic]
            peak = max(row[:cols]) or 1
            cells = []
            for i in sample:
                v = row[i]
                if v == 0:
                    cells.append(" ")
                else:
                    level = min(len(DENSITY_CHARS) - 1, 1 + int(4 * v / peak))
                    cells.append(DENSITY_CHARS[level])
            lines.append(f"{topic:<{name_w}} |{''.join(cells)}|")

        return Timeline(
            t_start=0.0,
            t_end=round(self.t_end, 3),
            bucket_s=self.bucket_s * (cols / len(sample) if sample else 1),
            legend=(
                "█ busiest · ▓▒░· lighter · (blank) silent — "
                f"{len(sample)} columns of {self.bucket_s * (cols / max(len(sample), 1)):.2f}s"
            ),
            rows=lines,
            provenance=prov or Provenance(),
        )

    def state_bytes(self) -> int:
        return 8 * self.max_cols * len(self.rows) + 64
