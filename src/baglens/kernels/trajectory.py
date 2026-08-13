"""Trajectory and TF kernels."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

#: field paths tried in order when the caller does not name one
POSE_PATHS = (
    ("pose.pose.position.x", "pose.pose.position.y", "pose.pose.position.z"),
    ("pose.position.x", "pose.position.y", "pose.position.z"),
    ("position.x", "position.y", "position.z"),
    ("x", "y", "z"),
)


@dataclass
class Trajectory:
    t: np.ndarray
    x: np.ndarray
    y: np.ndarray
    z: np.ndarray

    def __len__(self) -> int:
        return int(self.t.size)


def path_length(x: np.ndarray, y: np.ndarray, z: np.ndarray | None = None) -> float:
    if x.size < 2:
        return 0.0
    dx, dy = np.diff(x), np.diff(y)
    if z is not None and z.size == x.size:
        dz = np.diff(z)
        return float(np.sum(np.sqrt(dx * dx + dy * dy + dz * dz)))
    return float(np.sum(np.sqrt(dx * dx + dy * dy)))


def speeds(t: np.ndarray, x: np.ndarray, y: np.ndarray) -> np.ndarray:
    if t.size < 2:
        return np.zeros(0)
    dt = np.diff(t)
    dt[dt <= 0] = np.nan
    return np.sqrt(np.diff(x) ** 2 + np.diff(y) ** 2) / dt


def stops(t: np.ndarray, v: np.ndarray, threshold: float = 0.05,
          min_duration_s: float = 1.0) -> list[tuple[float, float]]:
    """Windows where speed stayed under `threshold` for at least `min_duration_s`."""
    out: list[tuple[float, float]] = []
    start = None
    for i, speed in enumerate(v):
        moving = not (np.isfinite(speed) and speed < threshold)
        if not moving and start is None:
            start = t[i]
        elif moving and start is not None:
            if t[i] - start >= min_duration_s:
                out.append((float(start), float(t[i])))
            start = None
    if start is not None and t.size and t[-1] - start >= min_duration_s:
        out.append((float(start), float(t[-1])))
    return out


def curvature(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Menger curvature over consecutive triples. Robust enough for a summary statistic."""
    if x.size < 3:
        return np.zeros(0)
    out = np.zeros(x.size - 2)
    for i in range(x.size - 2):
        ax, ay = x[i], y[i]
        bx, by = x[i + 1], y[i + 1]
        cx, cy = x[i + 2], y[i + 2]
        area = abs((bx - ax) * (cy - ay) - (cx - ax) * (by - ay)) / 2.0
        la = math.hypot(bx - ax, by - ay)
        lb = math.hypot(cx - bx, cy - by)
        lc = math.hypot(cx - ax, cy - ay)
        denom = la * lb * lc
        out[i] = (4 * area / denom) if denom > 1e-12 else 0.0
    return out


def deviation(actual: Trajectory, planned: Trajectory) -> tuple[np.ndarray, float, float, float]:
    """Nearest-point distance from each actual sample to the planned polyline."""
    if len(actual) == 0 or len(planned) == 0:
        return np.zeros(0), 0.0, 0.0, 0.0
    px, py = planned.x, planned.y
    dists = np.empty(actual.x.size)
    for i in range(actual.x.size):
        d = np.hypot(px - actual.x[i], py - actual.y[i])
        dists[i] = d.min()
    worst_idx = int(np.argmax(dists))
    return dists, float(dists.mean()), float(dists.max()), float(actual.t[worst_idx])


@dataclass
class TfEdge:
    parent: str
    child: str
    count: int = 0
    first_t: float = 0.0
    last_t: float = 0.0
    max_gap_s: float = 0.0
    jumps: list[float] = field(default_factory=list)


def tf_edges(messages: Any, t0_ns: int) -> dict[tuple[str, str], TfEdge]:
    """Walk `/tf` messages and summarise every parent→child link.

    A missing or stale transform is a classic silent killer: everything downstream
    quietly uses the last known pose and nobody notices until the map is wrong.
    """
    edges: dict[tuple[str, str], TfEdge] = {}
    last_xyz: dict[tuple[str, str], tuple[float, float, float]] = {}
    for _tp, ts, msg in messages:
        t = (ts - t0_ns) / 1e9
        for tr in getattr(msg, "transforms", []) or []:
            header = getattr(tr, "header", None)
            parent = str(getattr(header, "frame_id", "") if header else "")
            child = str(getattr(tr, "child_frame_id", ""))
            key = (parent, child)
            edge = edges.get(key)
            if edge is None:
                edge = TfEdge(parent=parent, child=child, first_t=t)
                edges[key] = edge
            if edge.count:
                edge.max_gap_s = max(edge.max_gap_s, t - edge.last_t)
            edge.count += 1
            edge.last_t = t

            transform = getattr(tr, "transform", None)
            trans = getattr(transform, "translation", None) if transform else None
            if trans is not None:
                xyz = (float(getattr(trans, "x", 0.0)), float(getattr(trans, "y", 0.0)),
                       float(getattr(trans, "z", 0.0)))
                prev = last_xyz.get(key)
                if prev is not None:
                    jump = math.dist(prev, xyz)
                    if jump > 1.0 and len(edge.jumps) < 50:
                        edge.jumps.append(round(t, 3))
                last_xyz[key] = xyz
    return edges


def tf_roots(edges: dict[tuple[str, str], TfEdge]) -> list[str]:
    children = {c for _p, c in edges}
    parents = {p for p, _c in edges}
    return sorted(parents - children)
