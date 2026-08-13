"""Deterministic synthetic bag generator with injected, labelled faults.

Everything downstream depends on this: unit tests, integration tests, the
precision/recall harness, the evals, and the README demo. It is written *before*
the detectors on purpose — a detector validated against a fixture you wrote
afterwards is validated against your own assumptions.

Each bag ships a sidecar ``<name>.ground_truth.json``: fault kind, exact time
window, magnitude. Deterministic given a seed. Nothing is committed; CI regenerates.

    python -m tests.synth.generate --out /tmp/bags --matrix
"""

from __future__ import annotations

import argparse
import json
import math
import random
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from mcap.writer import CompressionType, Writer
from mcap_ros2._dynamic import serialize_dynamic

from .msgdefs import MSGDEFS

BASE_EPOCH_NS = 1_780_000_000_000_000_000  # a fixed, plausible wall clock


# --------------------------------------------------------------------------- specs


@dataclass
class TopicSpec:
    topic: str
    msg_type: str
    hz: float
    #: declared QoS deadline in seconds, written into channel metadata
    deadline_s: float | None = None
    jitter_cv: float = 0.02


DEFAULT_TOPICS: tuple[TopicSpec, ...] = (
    TopicSpec("/imu/data", "sensor_msgs/msg/Imu", 100.0, deadline_s=0.01),
    TopicSpec("/odom", "nav_msgs/msg/Odometry", 50.0),
    TopicSpec("/tf", "tf2_msgs/msg/TFMessage", 50.0),
    TopicSpec("/scan", "sensor_msgs/msg/LaserScan", 10.0),
    TopicSpec("/camera/image_raw", "sensor_msgs/msg/CompressedImage", 30.0),
    TopicSpec("/cmd_vel", "geometry_msgs/msg/Twist", 20.0),
    TopicSpec("/diagnostics", "diagnostic_msgs/msg/DiagnosticArray", 1.0),
    TopicSpec("/rosout", "rcl_interfaces/msg/Log", 2.0),
)

SMALL_TOPICS: tuple[TopicSpec, ...] = (
    TopicSpec("/imu/data", "sensor_msgs/msg/Imu", 100.0, deadline_s=0.01),
    TopicSpec("/odom", "nav_msgs/msg/Odometry", 50.0),
    TopicSpec("/scan", "sensor_msgs/msg/LaserScan", 10.0),
    TopicSpec("/camera/image_raw", "sensor_msgs/msg/CompressedImage", 30.0),
    TopicSpec("/cmd_vel", "geometry_msgs/msg/Twist", 20.0),
)


@dataclass
class Fault:
    """One injected fault. ``kind`` names the generator; the rest is ground truth."""

    kind: str
    topic: str | None = None
    topics: tuple[str, ...] = ()
    t_start: float = 0.0
    t_end: float = 0.0
    params: dict[str, Any] = field(default_factory=dict)
    #: which detector this fault is meant to exercise
    detector: str = ""


# ------------------------------------------------------------------- fault helpers


def topic_dropout(topic: str, t_start: float, duration: float) -> Fault:
    return Fault("topic_dropout", topic=topic, t_start=t_start, t_end=t_start + duration,
                 detector="gap")


def rate_degradation(topic: str, from_hz: float, to_hz: float, t_start: float,
                     ramp_duration: float) -> Fault:
    return Fault("rate_degradation", topic=topic, t_start=t_start,
                 t_end=t_start + ramp_duration,
                 params={"from_hz": from_hz, "to_hz": to_hz}, detector="rate_degradation")


def jitter_injection(topic: str, cv_target: float, t_start: float, duration: float) -> Fault:
    return Fault("jitter_injection", topic=topic, t_start=t_start, t_end=t_start + duration,
                 params={"cv_target": cv_target}, detector="jitter")


def diffuse_drops(topic: str, drop_probability: float, t_start: float = 0.0,
                  duration: float | None = None) -> Fault:
    return Fault("diffuse_drops", topic=topic, t_start=t_start,
                 t_end=t_start + duration if duration else 0.0,
                 params={"p": drop_probability}, detector="dropped")


def recorder_lag(lag_growth_ms_per_min: float, t_start: float = 0.0) -> Fault:
    return Fault("recorder_lag", t_start=t_start,
                 params={"growth_ms_per_min": lag_growth_ms_per_min}, detector="clock_lag")


def clock_step(t: float, step_ms: float, direction: str = "forward") -> Fault:
    return Fault("clock_step", t_start=t, t_end=t,
                 params={"step_ms": step_ms, "direction": direction}, detector="clock_step")


def correlated_stall(topic_set: tuple[str, ...], t_start: float, duration: float) -> Fault:
    return Fault("correlated_stall", topics=tuple(topic_set), t_start=t_start,
                 t_end=t_start + duration, detector="correlation")


def truncation(truncate_at_fraction: float) -> Fault:
    return Fault("truncation", params={"fraction": truncate_at_fraction}, detector="file_integrity")


# ------------------------------------------------------------------ schedule build


def _schedule(hz: float, duration: float, rng: random.Random, cv: float,
              rate_fn=None, cv_fn=None, t0: float = 0.0) -> list[float]:
    """Inter-arrival driven schedule.

    ``rate_fn(t) -> hz`` makes the rate time-varying (D3); ``cv_fn(t) -> cv`` makes the
    spacing variance time-varying (D4). Inter-arrivals are drawn lognormal rather than
    Gaussian: message spacing cannot be negative, and a Gaussian at cv=0.6 produces
    negative gaps that have to be sorted away, quietly destroying the very variance the
    fault is supposed to inject.
    """
    out: list[float] = []
    t = t0 + rng.random() / hz
    while t < duration:
        out.append(t)
        cur_hz = max(rate_fn(t) if rate_fn else hz, 1e-3)
        period = 1.0 / cur_hz
        cur_cv = cv_fn(t) if cv_fn else cv
        if cur_cv <= 0:
            dt = period
        else:
            sigma = math.sqrt(math.log(1.0 + cur_cv * cur_cv))
            dt = period * math.exp(rng.gauss(0.0, sigma) - sigma * sigma / 2)
        t += max(dt, period * 0.02)
    return out


def _apply_faults(
    topics: tuple[TopicSpec, ...], duration: float, faults: list[Fault], rng: random.Random
) -> dict[str, list[tuple[float, float]]]:
    """Build per-topic [(publish_t, log_t)] schedules with every timing fault applied."""
    # 1. rate profile per topic (D3)
    rate_fns: dict[str, Any] = {}
    for f in faults:
        if f.kind == "rate_degradation" and f.topic:
            a, b = f.params["from_hz"], f.params["to_hz"]
            t0, t1 = f.t_start, f.t_end

            def make(a=a, b=b, t0=t0, t1=t1):
                def fn(t: float) -> float:
                    if t <= t0:
                        return a
                    if t >= t1:
                        return b
                    return a + (b - a) * (t - t0) / max(t1 - t0, 1e-9)

                return fn

            rate_fns[f.topic] = make()

    # 2. jitter profile per topic (D4) — widen inter-arrival variance in a window
    cv_fns: dict[str, Any] = {}
    for f in faults:
        if f.kind != "jitter_injection" or not f.topic:
            continue
        spec = next(s for s in topics if s.topic == f.topic)

        def make_cv(base=spec.jitter_cv, target=f.params["cv_target"],
                    t0=f.t_start, t1=f.t_end):
            def fn(t: float) -> float:
                return target if t0 <= t <= t1 else base

            return fn

        cv_fns[f.topic] = make_cv()

    sched: dict[str, list[float]] = {}
    for spec in topics:
        sched[spec.topic] = _schedule(
            spec.hz, duration, rng, spec.jitter_cv,
            rate_fns.get(spec.topic), cv_fns.get(spec.topic),
        )

    # 3. dropouts and stalls (D2, D7)
    for f in faults:
        targets: list[str] = []
        if f.kind == "topic_dropout" and f.topic:
            targets = [f.topic]
        elif f.kind == "correlated_stall":
            targets = list(f.topics)
        for tp in targets:
            sched[tp] = [t for t in sched[tp] if not (f.t_start <= t < f.t_end)]

    # 4. diffuse drops (D5)
    for f in faults:
        if f.kind != "diffuse_drops" or not f.topic:
            continue
        p = f.params["p"]
        lo = f.t_start
        hi = f.t_end if f.t_end > f.t_start else duration
        sched[f.topic] = [
            t for t in sched[f.topic] if not (lo <= t <= hi and rng.random() < p)
        ]

    # 5. publish → log time mapping: recorder lag (D6b) and clock steps (D6a/6c)
    lag_growth = 0.0
    lag_t0 = 0.0
    for f in faults:
        if f.kind == "recorder_lag":
            lag_growth = f.params["growth_ms_per_min"] / 1000.0 / 60.0
            lag_t0 = f.t_start
    steps = [
        (f.t_start, f.params["step_ms"] / 1000.0 * (-1 if f.params["direction"] == "backward" else 1))
        for f in faults
        if f.kind == "clock_step"
    ]

    out: dict[str, list[tuple[float, float]]] = {}
    for tp, times in sched.items():
        pairs = []
        for t in times:
            log_t = t
            if lag_growth and t > lag_t0:
                log_t += lag_growth * (t - lag_t0)
            for st, delta in steps:
                if t >= st:
                    log_t += delta
            pairs.append((t, log_t))
        out[tp] = pairs
    return out


# ---------------------------------------------------------------------- payloads


def _tiny_jpeg() -> bytes:
    """A real 8x8 grey JPEG — small enough to write 10k of them, real enough to decode."""
    global _JPEG_CACHE
    if _JPEG_CACHE is None:
        import io

        from PIL import Image

        buf = io.BytesIO()
        Image.new("L", (8, 8), 128).save(buf, format="JPEG", quality=40)
        _JPEG_CACHE = buf.getvalue()
    return _JPEG_CACHE


_JPEG_CACHE: bytes | None = None

_LOG_LINES = [
    ("controller_server", "Passing new path to controller"),
    ("planner_server", "Creating plan to goal"),
    ("amcl", "Requesting initial pose"),
    ("bt_navigator", "Begin navigating"),
]


def _stamp(t: float) -> dict[str, int]:
    total = BASE_EPOCH_NS + int(t * 1e9)
    return {"sec": total // 1_000_000_000, "nanosec": total % 1_000_000_000}


def _header(t: float, frame: str) -> dict[str, Any]:
    return {"stamp": _stamp(t), "frame_id": frame}


def _payload(msg_type: str, t: float, i: int, rng: random.Random) -> dict[str, Any]:
    """A payload that is cheap to build but has real, analysable numeric structure."""
    if msg_type == "sensor_msgs/msg/Imu":
        return {
            "header": _header(t, "imu_link"),
            "orientation": {"x": 0.0, "y": 0.0, "z": math.sin(t / 20), "w": math.cos(t / 20)},
            "orientation_covariance": [0.0] * 9,
            "angular_velocity": {"x": 0.0, "y": 0.0, "z": 0.1 * math.sin(t / 5) + rng.gauss(0, 0.01)},
            "angular_velocity_covariance": [0.0] * 9,
            "linear_acceleration": {
                "x": 0.2 * math.sin(t / 3) + rng.gauss(0, 0.05),
                "y": rng.gauss(0, 0.05),
                "z": 9.81 + rng.gauss(0, 0.05),
            },
            "linear_acceleration_covariance": [0.0] * 9,
        }
    if msg_type == "nav_msgs/msg/Odometry":
        return {
            "header": _header(t, "odom"),
            "child_frame_id": "base_link",
            "pose": {
                "pose": {
                    "position": {"x": 0.5 * t, "y": 2.0 * math.sin(t / 10), "z": 0.0},
                    "orientation": {"x": 0.0, "y": 0.0, "z": math.sin(t / 20), "w": math.cos(t / 20)},
                },
                "covariance": [0.0] * 36,
            },
            "twist": {
                "twist": {
                    "linear": {"x": 0.5 + rng.gauss(0, 0.01), "y": 0.0, "z": 0.0},
                    "angular": {"x": 0.0, "y": 0.0, "z": 0.2 * math.cos(t / 10)},
                },
                "covariance": [0.0] * 36,
            },
        }
    if msg_type == "geometry_msgs/msg/Twist":
        return {
            "linear": {"x": 0.5 + 0.1 * math.sin(t / 7), "y": 0.0, "z": 0.0},
            "angular": {"x": 0.0, "y": 0.0, "z": 0.2 * math.cos(t / 10)},
        }
    if msg_type == "sensor_msgs/msg/LaserScan":
        n = 36
        return {
            "header": _header(t, "laser"),
            "angle_min": -math.pi,
            "angle_max": math.pi,
            "angle_increment": 2 * math.pi / n,
            "time_increment": 0.0,
            "scan_time": 0.1,
            "range_min": 0.1,
            "range_max": 30.0,
            "ranges": [3.0 + 0.5 * math.sin(k / 3 + t) for k in range(n)],
            "intensities": [],
        }
    if msg_type == "sensor_msgs/msg/CompressedImage":
        return {"header": _header(t, "camera"), "format": "jpeg", "data": _tiny_jpeg()}
    if msg_type == "tf2_msgs/msg/TFMessage":
        return {
            "transforms": [
                {
                    "header": _header(t, "odom"),
                    "child_frame_id": "base_link",
                    "transform": {
                        "translation": {"x": 0.5 * t, "y": 2.0 * math.sin(t / 10), "z": 0.0},
                        "rotation": {"x": 0.0, "y": 0.0, "z": math.sin(t / 20), "w": math.cos(t / 20)},
                    },
                }
            ]
        }
    if msg_type == "rcl_interfaces/msg/Log":
        node, text = _LOG_LINES[i % len(_LOG_LINES)]
        return {
            "stamp": _stamp(t),
            "level": 20,
            "name": node,
            "msg": f"{text} (seq {i})",
            "file": "nav2.cpp",
            "function": "tick",
            "line": 42,
        }
    if msg_type == "diagnostic_msgs/msg/DiagnosticArray":
        return {
            "header": _header(t, ""),
            # SimpleNamespace, not a dict: the CDR encoder resolves fields by attribute
            # first, and a field literally named "values" collides with dict.values
            "status": [
                SimpleNamespace(
                    level=0,
                    name="motors",
                    message="OK",
                    hardware_id="base",
                    values=[{"key": "temp_c", "value": f"{40 + 5 * math.sin(t / 30):.1f}"}],
                )
            ],
        }
    return {"data": float(i)}


def _qos_yaml(deadline_s: float | None) -> str:
    if deadline_s is None:
        return ""
    sec = int(deadline_s)
    nsec = int((deadline_s - sec) * 1e9)
    return (
        "- history: 1\n  depth: 10\n  reliability: 1\n  durability: 2\n"
        f"  deadline:\n    sec: {sec}\n    nsec: {nsec}\n"
        "  lifespan:\n    sec: 0\n    nsec: 0\n"
        "  liveliness: 1\n  liveliness_lease_duration:\n    sec: 0\n    nsec: 0\n"
        "  avoid_ros_namespace_conventions: false\n"
    )


# ------------------------------------------------------------------------ writing


def generate_bag(
    path: str | Path,
    *,
    seed: int = 0,
    duration_s: float = 120.0,
    topics: tuple[TopicSpec, ...] = SMALL_TOPICS,
    faults: list[Fault] | None = None,
    compression: bool = True,
) -> dict[str, Any]:
    """Write one MCAP with the given faults injected. Returns the ground truth dict."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    faults = list(faults or [])
    rng = random.Random(seed)

    schedules = _apply_faults(topics, duration_s, faults, rng)

    encoders: dict[str, Any] = {}
    schema_ids: dict[str, int] = {}
    channel_ids: dict[str, int] = {}

    with path.open("wb") as f:
        writer = Writer(
            f,
            compression=CompressionType.ZSTD if compression else CompressionType.NONE,
            # small enough that a truncated file still has complete, recoverable chunks —
            # which is what a real interrupted recording looks like
            chunk_size=256 << 10,
        )
        writer.start(profile="ros2", library="baglens-synth")

        for spec in topics:
            msgdef = MSGDEFS[spec.msg_type]
            if spec.msg_type not in schema_ids:
                schema_ids[spec.msg_type] = writer.register_schema(
                    spec.msg_type, "ros2msg", msgdef.encode()
                )
                encoders[spec.msg_type] = serialize_dynamic(spec.msg_type, msgdef)[spec.msg_type]
            meta = {}
            qos = _qos_yaml(spec.deadline_s)
            if qos:
                meta["offered_qos_profiles"] = qos
            channel_ids[spec.topic] = writer.register_channel(
                topic=spec.topic,
                message_encoding="cdr",
                schema_id=schema_ids[spec.msg_type],
                metadata=meta,
            )

        type_of = {s.topic: s.msg_type for s in topics}
        seqs: dict[str, int] = dict.fromkeys(type_of, 0)

        merged: list[tuple[float, float, str]] = []
        for tp, pairs in schedules.items():
            merged.extend((log_t, pub_t, tp) for pub_t, log_t in pairs)
        merged.sort()

        for log_t, pub_t, tp in merged:
            i = seqs[tp]
            seqs[tp] = i + 1
            data = encoders[type_of[tp]](_payload(type_of[tp], pub_t, i, rng))
            writer.add_message(
                channel_id=channel_ids[tp],
                log_time=BASE_EPOCH_NS + int(log_t * 1e9),
                publish_time=BASE_EPOCH_NS + int(pub_t * 1e9),
                data=data,
                sequence=i,
            )
        writer.finish()

    truth: dict[str, Any] = {
        "path": str(path),
        "seed": seed,
        "duration_s": duration_s,
        "base_epoch_ns": BASE_EPOCH_NS,
        "topics": [
            {"topic": s.topic, "msg_type": s.msg_type, "hz": s.hz, "count": len(schedules[s.topic])}
            for s in topics
        ],
        "faults": [asdict(f) for f in faults],
        "clean": not faults,
    }

    # truncation is a post-write byte operation, not a schedule one
    for f in faults:
        if f.kind == "truncation":
            raw = path.read_bytes()
            keep = max(1024, int(len(raw) * f.params["fraction"]))
            path.write_bytes(raw[:keep])
            truth["truncated_to_bytes"] = keep
            truth["original_bytes"] = len(raw)

    gt = path.with_suffix(".ground_truth.json")
    gt.write_text(json.dumps(truth, indent=2))
    return truth


# ------------------------------------------------------------------- fault matrix


def fault_matrix(n_faulted: int = 160, n_clean: int = 40, duration_s: float = 120.0
                 ) -> list[tuple[str, int, list[Fault]]]:
    """(name, seed, faults) for the precision/recall corpus. Deterministic."""
    cases: list[tuple[str, int, list[Fault]]] = []
    rng = random.Random(20260813)
    kinds = [
        "topic_dropout",
        "rate_degradation",
        "jitter_injection",
        "diffuse_drops",
        "recorder_lag",
        "clock_step",
        "correlated_stall",
        "truncation",
    ]
    per = max(1, n_faulted // len(kinds))
    idx = 0
    for kind in kinds:
        for _ in range(per):
            seed = 1000 + idx
            r = random.Random(seed)
            topic = r.choice(["/imu/data", "/odom", "/scan", "/camera/image_raw", "/cmd_vel"])
            t0 = r.uniform(20.0, duration_s * 0.6)
            if kind == "topic_dropout":
                faults = [topic_dropout(topic, t0, r.uniform(1.0, 12.0))]
            elif kind == "rate_degradation":
                hz = {"/imu/data": 100.0, "/odom": 50.0, "/scan": 10.0,
                      "/camera/image_raw": 30.0, "/cmd_vel": 20.0}[topic]
                drop = r.uniform(0.25, 0.5)
                faults = [rate_degradation(topic, hz, hz * (1 - drop), 15.0, duration_s - 25.0)]
            elif kind == "jitter_injection":
                faults = [jitter_injection(topic, r.uniform(0.4, 0.9), t0, r.uniform(15.0, 40.0))]
            elif kind == "diffuse_drops":
                faults = [diffuse_drops(topic, r.uniform(0.08, 0.3))]
            elif kind == "recorder_lag":
                faults = [recorder_lag(r.uniform(60.0, 200.0))]
            elif kind == "clock_step":
                faults = [clock_step(t0, r.uniform(600.0, 3000.0),
                                     r.choice(["forward", "backward"]))]
            elif kind == "correlated_stall":
                faults = [correlated_stall(("/imu/data", "/odom", "/scan", "/camera/image_raw"),
                                           t0, r.uniform(2.0, 10.0))]
            else:
                faults = [truncation(r.uniform(0.55, 0.9))]
            cases.append((f"{kind}_{idx:03d}", seed, faults))
            idx += 1
    for i in range(n_clean):
        cases.append((f"clean_{i:03d}", 5000 + i, []))
    rng.shuffle(cases)
    return cases


def generate_corpus(out_dir: str | Path, n_faulted: int = 160, n_clean: int = 40,
                    duration_s: float = 120.0, quiet: bool = False) -> list[dict[str, Any]]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    truths = []
    cases = fault_matrix(n_faulted, n_clean, duration_s)
    for i, (name, seed, faults) in enumerate(cases):
        truths.append(
            generate_bag(out / f"{name}.mcap", seed=seed, duration_s=duration_s, faults=faults)
        )
        if not quiet and (i + 1) % 25 == 0:
            print(f"  {i + 1}/{len(cases)} bags")
    return truths


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="generate synthetic bags with labelled faults")
    ap.add_argument("--out", default="/tmp/baglens-bags")
    ap.add_argument("--matrix", action="store_true", help="generate the full precision/recall corpus")
    ap.add_argument("--faulted", type=int, default=160)
    ap.add_argument("--clean", type=int, default=40)
    ap.add_argument("--duration", type=float, default=120.0)
    args = ap.parse_args(argv)
    out = Path(args.out)
    if args.matrix:
        generate_corpus(out, args.faulted, args.clean, args.duration)
        print(f"wrote {args.faulted + args.clean} bags to {out}")
    else:
        generate_bag(out / "clean.mcap", seed=1, duration_s=args.duration)
        print(f"wrote {out / 'clean.mcap'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
