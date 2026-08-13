"""`spatial.*` — trajectory and transform-tree analysis."""

from __future__ import annotations

from typing import Any

import numpy as np
from pydantic import BaseModel, Field

from ..kernels.trajectory import (
    POSE_PATHS,
    Trajectory,
    curvature,
    deviation,
    path_length,
    speeds,
    stops,
    tf_edges,
    tf_roots,
)
from ..provenance import Provenance
from ..readers import open_bag
from ..readers.base import dotted_get
from .common import resolve


class TrajectorySummary(BaseModel):
    topic: str
    samples: int
    duration_s: float
    path_length_m: float
    displacement_m: float
    mean_speed_ms: float
    max_speed_ms: float
    stops: list[tuple[float, float]] = Field(default_factory=list)
    stopped_fraction: float = 0.0
    mean_curvature: float = 0.0
    bounding_box: dict[str, float] = Field(default_factory=dict)
    provenance: Provenance = Field(default_factory=Provenance)


class DeviationReport(BaseModel):
    actual_topic: str
    planned_topic: str
    mean_deviation_m: float
    max_deviation_m: float
    worst_at_s: float
    samples: int
    verdict: str = ""
    provenance: Provenance = Field(default_factory=Provenance)


class TfLink(BaseModel):
    parent: str
    child: str
    count: int
    hz: float
    max_gap_s: float
    jumps: int
    stale: bool


class TfReport(BaseModel):
    roots: list[str] = Field(default_factory=list)
    links: list[TfLink] = Field(default_factory=list)
    disconnected: list[str] = Field(default_factory=list)
    problems: list[str] = Field(default_factory=list)
    provenance: Provenance = Field(default_factory=Provenance)


def _read_trajectory(path: Any, topic: str, field_prefix: str | None) -> tuple[Trajectory, float]:
    reader = open_bag(path)
    meta = reader.metadata()
    t0 = meta.start_time_ns
    ts: list[float] = []
    xs: list[float] = []
    ys: list[float] = []
    zs: list[float] = []
    candidates = [tuple(f"{field_prefix}.{a}" for a in ("x", "y", "z"))] if field_prefix else POSE_PATHS
    chosen: tuple[str, str, str] | None = None

    for _tp, ts_ns, msg in reader.messages([topic]):
        if chosen is None:
            for cand in candidates:
                if dotted_get(msg, cand[0]) is not None:
                    chosen = cand
                    break
            if chosen is None:
                continue
        x = dotted_get(msg, chosen[0])
        y = dotted_get(msg, chosen[1])
        z = dotted_get(msg, chosen[2]) or 0.0
        if x is None or y is None:
            continue
        ts.append((ts_ns - t0) / 1e9)
        xs.append(x)
        ys.append(y)
        zs.append(z)
    reader.close()
    return (
        Trajectory(np.asarray(ts), np.asarray(xs), np.asarray(ys), np.asarray(zs)),
        meta.duration_s,
    )


def register(mcp: Any) -> None:
    @mcp.tool(name="spatial.trajectory_summary")
    def trajectory_summary(
        path: str, topic: str = "/odom", field_prefix: str | None = None,
        stop_threshold_ms: float = 0.05,
    ) -> TrajectorySummary:
        """Path length, speeds, stops, curvature and bounding box for a pose topic.

        Field paths are auto-detected (nav_msgs/Odometry, PoseStamped and friends); pass
        `field_prefix` like "pose.pose.position" to override.
        """
        p = resolve(path)
        traj, duration = _read_trajectory(p, topic, field_prefix)
        v = speeds(traj.t, traj.x, traj.y)
        stop_windows = stops(traj.t[1:], v, stop_threshold_ms)
        stopped = sum(b - a for a, b in stop_windows)
        curv = curvature(traj.x, traj.y)
        disp = (
            float(np.hypot(traj.x[-1] - traj.x[0], traj.y[-1] - traj.y[0])) if len(traj) else 0.0
        )
        return TrajectorySummary(
            topic=topic,
            samples=len(traj),
            duration_s=round(duration, 3),
            path_length_m=round(path_length(traj.x, traj.y, traj.z), 3),
            displacement_m=round(disp, 3),
            mean_speed_ms=round(float(np.nanmean(v)), 4) if v.size else 0.0,
            max_speed_ms=round(float(np.nanmax(v)), 4) if v.size else 0.0,
            stops=[(round(a, 2), round(b, 2)) for a, b in stop_windows[:20]],
            stopped_fraction=round(stopped / duration, 4) if duration else 0.0,
            mean_curvature=round(float(curv.mean()), 5) if curv.size else 0.0,
            bounding_box={
                "min_x": round(float(traj.x.min()), 3) if len(traj) else 0.0,
                "max_x": round(float(traj.x.max()), 3) if len(traj) else 0.0,
                "min_y": round(float(traj.y.min()), 3) if len(traj) else 0.0,
                "max_y": round(float(traj.y.max()), 3) if len(traj) else 0.0,
            },
            provenance=Provenance(
                path=str(p), topics=[topic], time_range=(0.0, duration),
                method="trajectory_summary", sample_count=len(traj),
            ),
        )

    @mcp.tool(name="spatial.trajectory_deviation")
    def trajectory_deviation(
        path: str, actual_topic: str = "/odom", planned_topic: str = "/plan"
    ) -> DeviationReport:
        """Actual path versus planned path: mean and max deviation, and when it was worst.

        Use after a navigation complaint — "it took a strange route" becomes a number and
        a timestamp you can then pull keyframes for.
        """
        p = resolve(path)
        actual, duration = _read_trajectory(p, actual_topic, None)
        planned, _d = _read_trajectory(p, planned_topic, None)
        dists, mean_d, max_d, worst_t = deviation(actual, planned)
        verdict = (
            "planned path topic is empty or missing — cannot compare"
            if len(planned) == 0
            else f"max deviation {max_d:.2f} m at t={worst_t:.1f}s "
                 f"({'within tolerance' if max_d < 0.5 else 'significant excursion'})"
        )
        return DeviationReport(
            actual_topic=actual_topic, planned_topic=planned_topic,
            mean_deviation_m=round(mean_d, 4), max_deviation_m=round(max_d, 4),
            worst_at_s=round(worst_t, 3), samples=int(dists.size), verdict=verdict,
            provenance=Provenance(
                path=str(p), topics=[actual_topic, planned_topic], time_range=(0.0, duration),
                method="nearest_point_deviation", sample_count=int(dists.size),
            ),
        )

    @mcp.tool(name="spatial.tf_report")
    def tf_report(path: str, topic: str = "/tf", stale_factor: float = 10.0) -> TfReport:
        """The transform tree: every parent→child link, its rate, gaps and jumps.

        A stale or missing transform is a classic silent killer — everything downstream
        keeps using the last known pose and nothing reports it. Links whose largest gap
        exceeds `stale_factor` times their median period are flagged.
        """
        p = resolve(path)
        reader = open_bag(p)
        meta = reader.metadata()
        edges = tf_edges(reader.messages([topic]), meta.start_time_ns)
        reader.close()

        links: list[TfLink] = []
        problems: list[str] = []
        for (parent, child), e in sorted(edges.items()):
            span = max(e.last_t - e.first_t, 1e-9)
            hz = e.count / span
            stale = e.max_gap_s > stale_factor / max(hz, 1e-9)
            links.append(
                TfLink(parent=parent, child=child, count=e.count, hz=round(hz, 3),
                       max_gap_s=round(e.max_gap_s, 3), jumps=len(e.jumps), stale=stale)
            )
            if stale:
                problems.append(
                    f"{parent}→{child} went stale for {e.max_gap_s:.2f}s "
                    f"(publishing at {hz:.1f} Hz)"
                )
            if e.jumps:
                problems.append(
                    f"{parent}→{child} jumped more than 1 m {len(e.jumps)}x "
                    f"(first at t={e.jumps[0]:.1f}s)"
                )
            if e.last_t < meta.duration_s * 0.9:
                problems.append(
                    f"{parent}→{child} stopped publishing at t={e.last_t:.1f}s and never resumed"
                )

        children = {c for _p, c in edges}
        parents = {p_ for p_, _c in edges}
        return TfReport(
            roots=tf_roots(edges),
            links=links[:60],
            disconnected=sorted(children - parents - {c for _p, c in edges if _p in parents})[:20],
            problems=problems[:20],
            provenance=Provenance(
                path=str(p), topics=[topic], time_range=(0.0, meta.duration_s),
                method="tf_tree_walk", sample_count=sum(e.count for e in edges.values()),
            ),
        )
