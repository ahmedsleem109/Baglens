"""`frames.*` and `pointcloud.*` — the multimodal bridge (G8).

"Anomaly detected at t=412.3s" → "here are the three keyframes around it, as an image
a vision model can actually read."
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from ..config import CONFIG
from ..multimodal.frames import (
    contact_sheet,
    evenly_spaced,
    extract_frames,
    image_topics,
    pointcloud_summary,
    to_png_bytes,
)
from ..provenance import Provenance
from ..readers import open_bag
from .common import resolve


class FrameRef(BaseModel):
    t: float
    file: str
    width: int
    height: int


class FrameSet(BaseModel):
    topic: str
    frames: list[FrameRef] = Field(default_factory=list)
    contact_sheet_file: str | None = None
    note: str = ""
    provenance: Provenance = Field(default_factory=Provenance)


class PointcloudSummary(BaseModel):
    topic: str
    samples: int
    mean_points: float = 0.0
    min_points: float = 0.0
    max_points: float = 0.0
    empty_return_ratio: float = 0.0
    mean_bytes: float = 0.0
    verdict: str = ""
    provenance: Provenance = Field(default_factory=Provenance)


def _out_dir() -> Path:
    d = CONFIG.artifact_dir / "frames"
    d.mkdir(parents=True, exist_ok=True)
    return d


def register(mcp: Any) -> None:
    @mcp.tool(name="frames.extract_keyframes")
    def extract_keyframes(
        path: str,
        topic: str | None = None,
        times: list[float] | None = None,
        around_s: float | None = None,
        count: int = 3,
        window_s: float = 0.5,
    ) -> FrameSet:
        """Camera frames near given timestamps, written as PNG files you can open.

        Give `times` explicitly, or `around_s` with `count` to sample either side of an
        incident. Prefer frames.contact_sheet when you want to *compare* frames — one
        image is far cheaper than several and easier to reason across.
        """
        if not CONFIG.allow_frames:
            return FrameSet(topic=topic or "", note="image extraction is disabled (--no-frames)")
        p = resolve(path)
        reader = open_bag(p)
        meta = reader.metadata()
        reader.close()
        topic = topic or (image_topics(meta) or [""])[0]
        if not topic:
            return FrameSet(topic="", note="no image topics in this recording")

        if times is None:
            if around_s is None:
                times = evenly_spaced(0.0, meta.duration_s, count)
            else:
                half = window_s * max(count - 1, 1)
                times = evenly_spaced(max(0.0, around_s - half), around_s + half, count)

        frames = extract_frames(p, topic, times, window_s=max(window_s, 0.5), max_frames=12)
        out = _out_dir()
        refs = []
        for f in frames:
            fname = out / f"{Path(p).stem}_{topic.strip('/').replace('/', '_')}_{f.t:.3f}.png"
            fname.write_bytes(to_png_bytes(f.image))
            refs.append(FrameRef(t=round(f.t, 3), file=str(fname),
                                 width=f.image.width, height=f.image.height))
        return FrameSet(
            topic=topic,
            frames=refs,
            note=f"{len(refs)} frames written" if refs else "no decodable frames in that window",
            provenance=Provenance(
                path=str(p), topics=[topic],
                time_range=(min(times), max(times)) if times else (0.0, 0.0),
                method=f"nearest_frame(±{window_s}s)", sample_count=len(refs),
            ),
        )

    @mcp.tool(name="frames.contact_sheet")
    def contact_sheet_tool(
        path: str,
        topic: str | None = None,
        start_s: float | None = None,
        end_s: float | None = None,
        count: int = 9,
    ) -> FrameSet:
        """A grid of frames across a window as a single labelled image.

        One picture instead of twenty: cheaper in context and easier for a vision model
        to compare across, because the frames sit side by side with their timestamps.
        """
        if not CONFIG.allow_frames:
            return FrameSet(topic=topic or "", note="image extraction is disabled (--no-frames)")
        p = resolve(path)
        reader = open_bag(p)
        meta = reader.metadata()
        reader.close()
        topic = topic or (image_topics(meta) or [""])[0]
        if not topic:
            return FrameSet(topic="", note="no image topics in this recording")

        lo = 0.0 if start_s is None else start_s
        hi = meta.duration_s if end_s is None else end_s
        times = evenly_spaced(lo, hi, min(max(count, 1), 12))
        frames = extract_frames(p, topic, times, window_s=max((hi - lo) / max(count, 1), 0.5))
        sheet = contact_sheet(frames)
        target = _out_dir() / f"{Path(p).stem}_{topic.strip('/').replace('/', '_')}_sheet.png"
        target.write_bytes(to_png_bytes(sheet))
        return FrameSet(
            topic=topic,
            frames=[FrameRef(t=round(f.t, 3), file=str(target),
                             width=f.image.width, height=f.image.height) for f in frames],
            contact_sheet_file=str(target),
            note=f"{len(frames)} frames tiled into one image",
            provenance=Provenance(
                path=str(p), topics=[topic], time_range=(lo, hi),
                method=f"contact_sheet(n={len(frames)})", sample_count=len(frames),
            ),
        )

    @mcp.tool(name="pointcloud.summary")
    def pointcloud_summary_tool(path: str, topic: str, max_samples: int = 200) -> PointcloudSummary:
        """Point counts, density and empty-return ratio over a lidar topic.

        Summarised statistically rather than rendered: a falling point count or a rising
        empty-return ratio is sensor degradation, and that shows up in the numbers long
        before it shows up in a picture.
        """
        p = resolve(path)
        reader = open_bag(p)
        meta = reader.metadata()
        counts: list[float] = []
        sizes: list[float] = []
        empty = 0
        for n, (_tp, _ts, msg) in enumerate(reader.messages([topic]), start=1):
            stats = pointcloud_summary(msg)
            counts.append(stats["points"])
            sizes.append(stats["data_bytes"])
            if stats["points"] == 0:
                empty += 1
            if n >= max_samples:
                break
        reader.close()
        if not counts:
            return PointcloudSummary(
                topic=topic, samples=0,
                verdict="no PointCloud2 messages decoded on that topic",
                provenance=Provenance(path=str(p), topics=[topic], method="pointcloud_stats"),
            )
        mean_points = sum(counts) / len(counts)
        ratio = empty / len(counts)
        return PointcloudSummary(
            topic=topic, samples=len(counts),
            mean_points=round(mean_points, 1),
            min_points=round(min(counts), 1),
            max_points=round(max(counts), 1),
            empty_return_ratio=round(ratio, 4),
            mean_bytes=round(sum(sizes) / len(sizes), 1),
            verdict=(
                "sensor looks healthy" if ratio < 0.01 and min(counts) > 0.5 * mean_points
                else "point counts vary widely — check for occlusion or a failing sensor"
            ),
            provenance=Provenance(
                path=str(p), topics=[topic], time_range=(0.0, meta.duration_s),
                method="pointcloud_stats", sample_count=len(counts),
            ),
        )
