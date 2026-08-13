"""Keyframe extraction and contact sheets (G8).

A contact sheet is the budget-efficient form: twelve frames as one image with burnt-in
timestamps costs a fraction of twelve separate images and is easier for a VLM to reason
across, because the comparison is inside a single picture rather than across a
conversation.
"""

from __future__ import annotations

import io
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

IMAGE_TYPES = ("sensor_msgs/msg/Image", "sensor_msgs/msg/CompressedImage")

ENCODING_MODES = {
    "mono8": ("L", 1),
    "8UC1": ("L", 1),
    "rgb8": ("RGB", 3),
    "bgr8": ("RGB", 3),
    "8UC3": ("RGB", 3),
    "rgba8": ("RGBA", 4),
    "bgra8": ("RGBA", 4),
}


@dataclass
class Frame:
    t: float
    topic: str
    image: Image.Image


def decode(msg: Any, topic: str, t: float) -> Frame | None:
    """Decode CompressedImage or raw Image into PIL, without OpenCV."""
    data = getattr(msg, "data", None)
    if data is None:
        return None
    raw = bytes(data)
    fmt = getattr(msg, "format", None)
    if fmt:  # CompressedImage
        try:
            return Frame(t, topic, Image.open(io.BytesIO(raw)).convert("RGB"))
        except Exception:
            return None

    encoding = str(getattr(msg, "encoding", "") or "")
    width = int(getattr(msg, "width", 0) or 0)
    height = int(getattr(msg, "height", 0) or 0)
    mode_channels = ENCODING_MODES.get(encoding)
    if not (mode_channels and width and height):
        return None
    mode, channels = mode_channels
    expected = width * height * channels
    if len(raw) < expected:
        return None
    img = Image.frombytes(mode, (width, height), raw[:expected])
    if encoding.startswith("bgr"):
        b, g, r, *rest = img.split()
        img = Image.merge("RGB", (r, g, b)) if not rest else Image.merge("RGBA", (r, g, b, rest[0]))
    return Frame(t, topic, img.convert("RGB"))


def image_topics(meta: Any) -> list[str]:
    return [t.topic for t in meta.topics if t.msg_type in IMAGE_TYPES]


def extract_frames(
    path: str | Path,
    topic: str,
    times: list[float],
    window_s: float = 0.5,
    max_frames: int = 12,
) -> list[Frame]:
    """Frames nearest the requested timestamps. One pass, nearest-match per target."""
    from ..readers import open_bag

    reader = open_bag(path)
    meta = reader.metadata()
    t0 = meta.start_time_ns
    targets = sorted(times)[:max_frames]
    best: dict[float, tuple[float, Any]] = {}
    lo = max(0.0, min(targets) - window_s) if targets else 0.0
    hi = (max(targets) + window_s) if targets else meta.duration_s

    for _tp, ts, msg in reader.messages([topic], lo, hi):
        rel = (ts - t0) / 1e9
        for target in targets:
            if abs(rel - target) > window_s:
                continue
            current = best.get(target)
            if current is None or abs(rel - target) < abs(current[0] - target):
                best[target] = (rel, msg)
    reader.close()

    frames: list[Frame] = []
    for target in targets:
        hit = best.get(target)
        if hit is None:
            continue
        frame = decode(hit[1], topic, hit[0])
        if frame is not None:
            frames.append(frame)
    return frames


def evenly_spaced(start_s: float, end_s: float, n: int) -> list[float]:
    if n <= 1:
        return [start_s]
    step = (end_s - start_s) / (n - 1)
    return [start_s + i * step for i in range(n)]


def contact_sheet(frames: list[Frame], cell_width: int = 240, cols: int | None = None
                  ) -> Image.Image:
    """Tile frames into one labelled image. Twelve pictures for the price of one."""
    if not frames:
        return Image.new("RGB", (cell_width, 60), (30, 30, 30))
    cols = cols or min(4, len(frames))
    rows = math.ceil(len(frames) / cols)
    ratio = frames[0].image.height / max(frames[0].image.width, 1)
    cell_h = int(cell_width * ratio) + 18

    sheet = Image.new("RGB", (cols * cell_width, rows * cell_h), (24, 24, 27))
    draw = ImageDraw.Draw(sheet)
    for i, frame in enumerate(frames):
        col, row = i % cols, i // cols
        thumb = frame.image.resize((cell_width, int(cell_width * ratio)))
        x, y = col * cell_width, row * cell_h
        sheet.paste(thumb, (x, y))
        draw.text((x + 4, y + int(cell_width * ratio) + 3), f"t={frame.t:.2f}s",
                  fill=(230, 230, 230))
    return sheet


def to_png_bytes(image: Image.Image) -> bytes:
    buf = io.BytesIO()
    image.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def pointcloud_summary(msg: Any) -> dict[str, float]:
    """Cheap structural statistics for PointCloud2 without unpacking every point."""
    width = int(getattr(msg, "width", 0) or 0)
    height = int(getattr(msg, "height", 0) or 0)
    point_step = int(getattr(msg, "point_step", 0) or 0)
    data = getattr(msg, "data", b"") or b""
    return {
        "points": float(width * height),
        "width": float(width),
        "height": float(height),
        "point_step_bytes": float(point_step),
        "data_bytes": float(len(data)),
        "is_dense": 1.0 if getattr(msg, "is_dense", False) else 0.0,
    }
