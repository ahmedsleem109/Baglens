"""bag → catalog rows + Parquet signal cache.

Indexing runs the same single-pass auditor the health tools use, so a mission costs
exactly one read of its timing records. Numeric signals are a second, optional pass
that decodes only the topics named in ``SIGNAL_MAP`` — decoding is what costs money,
so it is opt-in and narrow by construction.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from ..config import CONFIG
from ..detectors import Auditor
from ..provenance import mission_id_for
from ..readers import open_bag
from .store import INDEX_VERSION, Catalog

#: message type → numeric field paths worth caching for cross-mission comparison.
#: Deliberately short: a signal nobody compares is a Parquet file nobody reads.
SIGNAL_MAP: dict[str, tuple[str, ...]] = {
    "nav_msgs/msg/Odometry": (
        "twist.twist.linear.x",
        "twist.twist.angular.z",
        "pose.pose.position.x",
        "pose.pose.position.y",
    ),
    "geometry_msgs/msg/Twist": ("linear.x", "angular.z"),
    "sensor_msgs/msg/Imu": (
        "linear_acceleration.x",
        "linear_acceleration.z",
        "angular_velocity.z",
    ),
    "sensor_msgs/msg/LaserScan": ("range_min", "range_max"),
    "sensor_msgs/msg/BatteryState": ("voltage", "percentage"),
}

BAG_GLOBS = ("*.mcap", "*.db3", "*.bag", "*.ulg")


@dataclass
class IndexStatus:
    total: int = 0
    done: int = 0
    failed: int = 0
    running: bool = False
    current: str = ""
    started_at: float = 0.0
    errors: list[str] = field(default_factory=list)

    @property
    def progress(self) -> float:
        return self.done / self.total if self.total else 1.0

    @property
    def elapsed_s(self) -> float:
        return time.time() - self.started_at if self.started_at else 0.0

    @property
    def eta_s(self) -> float:
        if not self.done or not self.running:
            return 0.0
        return (self.elapsed_s / self.done) * (self.total - self.done)


STATUS = IndexStatus()
_LOCK = threading.Lock()


def discover(root: str | Path, pattern: str | None = None) -> list[Path]:
    p = Path(root).expanduser()
    if p.is_file():
        return [p]
    globs = (pattern,) if pattern else BAG_GLOBS
    out: list[Path] = []
    for g in globs:
        out.extend(p.rglob(g))
    # a rosbag2 directory holds its .db3 next to metadata.yaml; keep the file, not the dir
    return sorted({q for q in out if q.is_file()})


def _robot_id(path: Path, meta: Any) -> str:
    """Best-effort robot identity: an explicit metadata field, else the parent directory."""
    for topic in meta.topics:
        rid = topic.qos.get("robot_id") if isinstance(topic.qos, dict) else None
        if rid:
            return str(rid)
    return path.parent.name


def index_mission(
    path: str | Path,
    catalog: Catalog,
    with_signals: bool = True,
    report: Any = None,
    robot_id: str | None = None,
) -> str:
    """Audit a recording and write it into the catalog.

    `report` skips the audit when the caller already has one — a monitor that watched the
    mission live has, and re-reading a 2 GB recording at landing to reach a conclusion it
    already holds is the difference between ingestion taking seconds and taking minutes.
    `robot_id` overrides the directory-name guess, which is the whole point on a vehicle:
    the fleet layer is only as good as its identities.
    """
    path = Path(path)
    mission_id = mission_id_for(path)
    reader = open_bag(path)
    if report is None:
        report = Auditor(reader).run()
    meta = reader.metadata()

    start = datetime.fromtimestamp(meta.start_time_ns / 1e9) if meta.start_time_ns else None
    end = datetime.fromtimestamp(meta.end_time_ns / 1e9) if meta.end_time_ns else None

    catalog.upsert_mission(
        {
            "mission_id": mission_id,
            "path": str(path),
            "format": meta.format,
            "robot_id": robot_id or _robot_id(path, meta),
            "start_time": start,
            "end_time": end,
            "duration_s": meta.duration_s,
            "message_count": meta.message_count,
            "size_bytes": meta.size_bytes,
            "health_score": report.overall_score,
            "verdict": report.verdict,
            "metadata": {
                "partial": meta.partial,
                "in_progress": meta.in_progress,
                "warnings": meta.warnings,
                "caveats": report.caveats,
            },
        }
    )

    qos_by_topic = {t.topic: t.qos for t in meta.topics}
    catalog.replace_rows(
        "topics",
        mission_id,
        [
            {
                "mission_id": mission_id,
                "topic": t.topic,
                "msg_type": t.msg_type,
                "count": t.count,
                "expected_hz": t.expected_hz,
                "actual_hz": t.observed_hz,
                "hz_source": t.hz_source,
                "jitter_cv": t.jitter_cv,
                "gap_count": t.gap_count,
                "max_gap_s": t.max_gap_s,
                "total_silent_s": t.total_silent_s,
                "estimated_dropped": t.estimated_dropped,
                "score": t.score,
                "qos": json.dumps(qos_by_topic.get(t.topic, {})),
            }
            for t in report.topics
        ],
    )

    catalog.replace_rows(
        "events",
        mission_id,
        [
            {
                "mission_id": mission_id,
                "finding_id": f.id,
                "t": f.t_start,
                "t_end": f.t_end,
                "kind": f.detector,
                "topic": f.topic,
                "severity": int(f.severity),
                "summary": f.summary,
                "detail": json.dumps(f.evidence),
            }
            for f in report.findings
        ],
    )

    if with_signals:
        _index_signals(path, mission_id, report, catalog)

    _index_log_patterns(path, mission_id, report, catalog)
    reader.close()
    return mission_id


def _index_signals(path: Path, mission_id: str, report: Any, catalog: Catalog) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    out_dir = CONFIG.signal_dir / mission_id
    out_dir.mkdir(parents=True, exist_ok=True)
    reader = open_bag(path)
    rows: list[dict[str, Any]] = []

    for topic_health in report.topics:
        paths = SIGNAL_MAP.get(topic_health.msg_type)
        if not paths:
            continue
        series: dict[str, tuple[list[float], list[float]]] = {p: ([], []) for p in paths}
        t0 = reader.metadata().start_time_ns
        from ..readers.base import dotted_get

        for _tp, ts, msg in reader.messages([topic_health.topic]):
            rel = (ts - t0) / 1e9
            for fp in paths:
                v = dotted_get(msg, fp)
                if v is not None:
                    series[fp][0].append(rel)
                    series[fp][1].append(v)

        for fp, (ts_list, vs_list) in series.items():
            if len(vs_list) < 2:
                continue
            key = f"{topic_health.topic}.{fp}"
            fname = key.strip("/").replace("/", "_") + ".parquet"
            target = out_dir / fname
            pq.write_table(
                pa.table({"t": ts_list, "v": vs_list}), target, compression="zstd"
            )
            arr = np.asarray(vs_list)
            q = np.percentile(arr, [50, 95, 99])
            rows.append(
                {
                    "mission_id": mission_id,
                    "signal_key": key,
                    "parquet_path": str(target),
                    "sample_hz": len(vs_list) / max(ts_list[-1] - ts_list[0], 1e-9),
                    "n": len(vs_list),
                    "min": float(arr.min()),
                    "max": float(arr.max()),
                    "mean": float(arr.mean()),
                    "stddev": float(arr.std(ddof=1)) if arr.size > 1 else 0.0,
                    "p50": float(q[0]),
                    "p95": float(q[1]),
                    "p99": float(q[2]),
                }
            )
    reader.close()
    catalog.replace_rows("signals", mission_id, rows)


def _index_log_patterns(path: Path, mission_id: str, report: Any, catalog: Catalog) -> None:
    from ..kernels.logs import cluster_templates, read_log_messages

    log_topics = [
        t.topic for t in report.topics if t.msg_type in ("rcl_interfaces/msg/Log", "rosgraph_msgs/Log")
    ]
    if not log_topics:
        catalog.replace_rows("log_patterns", mission_id, [])
        return
    entries = read_log_messages(path, log_topics)
    clusters = cluster_templates(entries)
    catalog.replace_rows(
        "log_patterns",
        mission_id,
        [
            {
                "mission_id": mission_id,
                "template": c.template,
                "level": c.level,
                "count": c.count,
                "example": c.example,
            }
            for c in clusters
        ],
    )


def index_paths(
    paths: list[Path], catalog: Catalog, force: bool = False, with_signals: bool = True
) -> IndexStatus:
    """Index a list of bags, updating the module-level STATUS as it goes."""
    global STATUS
    with _LOCK:
        STATUS = IndexStatus(total=len(paths), running=True, started_at=time.time())
    known = catalog.indexed_ids()

    for p in paths:
        STATUS.current = str(p)
        try:
            if not force:
                existing = catalog.mission_by_path(str(p))
                if existing and existing.get("index_version") == INDEX_VERSION:
                    STATUS.done += 1
                    continue
                if mission_id_for(p) in known and not existing:
                    pass
            index_mission(p, catalog, with_signals=with_signals)
        except Exception as exc:  # a bad bag must not stop the corpus
            STATUS.failed += 1
            if len(STATUS.errors) < 20:
                STATUS.errors.append(f"{p.name}: {type(exc).__name__}: {exc}")
        STATUS.done += 1

    STATUS.running = False
    STATUS.current = ""
    return STATUS


def index_in_background(paths: list[Path], catalog: Catalog, force: bool = False,
                        with_signals: bool = True) -> IndexStatus:
    thread = threading.Thread(
        target=index_paths, args=(paths, catalog, force, with_signals), daemon=True
    )
    with _LOCK:
        globals()["STATUS"] = IndexStatus(total=len(paths), running=True, started_at=time.time())
    thread.start()
    return STATUS
