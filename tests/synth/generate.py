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
from dataclasses import asdict, dataclass, field, replace
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
    #: 1 = RELIABLE, 2 = BEST_EFFORT (the profile that permits silent drops)
    reliability: int = 1
    #: KEEP_LAST queue depth; a shallow queue on a fast topic discards under load
    depth: int = 10
    #: seconds between capture and publish, written into `header.stamp` as an offset from
    #: the publish time. 0.0 keeps the historical behaviour — stamp equals publish — which
    #: every fixture predating F1 relies on. A real sensor is never 0.0, so the topic sets
    #: that exercise data age set it explicitly.
    capture_delay_s: float = 0.0


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

#: profiles that permit silent loss: BEST_EFFORT on a fast sensor, a depth-1 queue on a
#: fast one, and a deadline the publisher does not honour
LOSSY_QOS_TOPICS: tuple[TopicSpec, ...] = (
    TopicSpec("/imu/data", "sensor_msgs/msg/Imu", 100.0, deadline_s=0.01, reliability=2),
    TopicSpec("/scan", "sensor_msgs/msg/LaserScan", 10.0, depth=1),
    TopicSpec("/camera/image_raw", "sensor_msgs/msg/CompressedImage", 30.0, depth=2,
              reliability=2),
    TopicSpec("/odom", "nav_msgs/msg/Odometry", 50.0, deadline_s=1.0),
    TopicSpec("/cmd_vel", "geometry_msgs/msg/Twist", 20.0),
)

#: the sensor set that exercises the paths a camera-and-lidar robot actually uses:
#: raw (uncompressed) images, a point cloud, and a planned path to deviate from
SENSOR_TOPICS: tuple[TopicSpec, ...] = (
    TopicSpec("/odom", "nav_msgs/msg/Odometry", 50.0),
    TopicSpec("/plan", "nav_msgs/msg/Path", 1.0),
    TopicSpec("/camera/image_raw", "sensor_msgs/msg/Image", 10.0),
    TopicSpec("/points", "sensor_msgs/msg/PointCloud2", 10.0),
    TopicSpec("/cmd_vel", "geometry_msgs/msg/Twist", 20.0),
)

SMALL_TOPICS: tuple[TopicSpec, ...] = (
    TopicSpec("/imu/data", "sensor_msgs/msg/Imu", 100.0, deadline_s=0.01),
    TopicSpec("/odom", "nav_msgs/msg/Odometry", 50.0),
    TopicSpec("/scan", "sensor_msgs/msg/LaserScan", 10.0),
    TopicSpec("/camera/image_raw", "sensor_msgs/msg/CompressedImage", 30.0),
    TopicSpec("/cmd_vel", "geometry_msgs/msg/Twist", 20.0),
)


#: the graph a pre-flight gate (F2) is asked to approve. Small on purpose: the gate has
#: thirty seconds, and a baseline it cannot establish in that window is worse than useless.
#: Capture delays are set here because the gate checks a data-age budget: a fixture whose
#: sensors stamp at their own publish time reports an age of zero, and the age check would
#: be untested dead code. These are the sort of numbers real drivers produce.
PREFLIGHT_TOPICS: tuple[TopicSpec, ...] = (
    TopicSpec("/imu/data", "sensor_msgs/msg/Imu", 100.0, deadline_s=0.01,
              capture_delay_s=0.002),
    TopicSpec("/odom", "nav_msgs/msg/Odometry", 50.0, capture_delay_s=0.004),
    TopicSpec("/scan", "sensor_msgs/msg/LaserScan", 10.0, capture_delay_s=0.030),
    TopicSpec("/camera/image_raw", "sensor_msgs/msg/CompressedImage", 30.0,
              capture_delay_s=0.018),
)


def preflight_scenario(kind: str) -> tuple[tuple[TopicSpec, ...], list[Fault]]:
    """(topics, faults) for one pre-flight scenario, named by what is wrong with it.

    Shared by the unit tests and the eval so that "healthy" means one thing. Every
    scenario is expressible with the existing primitives — a missing topic is a shorter
    topic list, a halved rate is a different `hz` — which is why F2 needs no new fixture
    machinery, only an agreed table of cases.
    """
    topics = PREFLIGHT_TOPICS
    if kind == "healthy":
        return topics, []
    if kind == "missing_topic":
        return tuple(s for s in topics if s.topic != "/scan"), []
    if kind == "halved_rate":
        # `replace`, not a positional rebuild: the latter silently drops any field added
        # to TopicSpec later, which is how a fixture stops testing what it claims to
        return tuple(
            replace(s, hz=s.hz / 2) if s.topic == "/scan" else s for s in topics
        ), []
    if kind == "clock_skew":
        return topics, [clock_step(12.0, 800.0)]
    if kind == "already_degrading":
        return topics, [rate_degradation("/camera/image_raw", 30.0, 12.0, 4.0, 24.0)]
    if kind == "silent_topic":
        return topics, [topic_dropout("/scan", 5.0, 25.0)]
    raise ValueError(f"unknown pre-flight scenario {kind!r}")


#: every scenario the gate is scored against. "healthy" must never fail; the rest must.
PREFLIGHT_SCENARIOS: tuple[str, ...] = (
    "healthy", "missing_topic", "halved_rate", "clock_skew",
    "already_degrading", "silent_topic",
)


@dataclass
class TfEdgeSpec:
    """One parent→child transform, and how it misbehaves (F3).

    `/tf` is many streams in one topic, so a TF fixture is a set of these rather than a
    `TopicSpec`. Every field here corresponds to a failure someone has actually lost a
    week to.
    """

    parent: str
    child: str
    hz: float = 20.0
    #: published on `/tf_static` instead of `/tf` — latched once, at the start
    static: bool = False
    #: seconds added to the transform's stamp. Positive means stamped into the *future*,
    #: which is what produces "lookup would require extrapolation into the future".
    stamp_offset_s: float = 0.0
    #: a second node publishing the same edge, slightly later and with a different value.
    #: Silent and vicious: consumers see the two fight, and nothing reports it.
    duplicate_publisher: bool = False
    #: how far the duplicate disagrees, in metres
    duplicate_disagreement_m: float = 0.35
    #: windows during which this edge is not published at all, breaking the chain
    gaps: tuple[tuple[float, float], ...] = ()
    #: never published at all — the static transform nobody remembered to launch
    absent: bool = False


#: a small, realistic tree: map -> odom -> base_link -> {laser, camera}
BASE_TF: tuple[TfEdgeSpec, ...] = (
    TfEdgeSpec("map", "odom", hz=20.0),
    TfEdgeSpec("odom", "base_link", hz=50.0),
    TfEdgeSpec("base_link", "laser", hz=0.0, static=True),
    TfEdgeSpec("base_link", "camera", hz=0.0, static=True),
)


def tf_scenario(kind: str) -> tuple[TfEdgeSpec, ...]:
    """The TF tree for one scenario, named by what is wrong with it."""
    edges = BASE_TF
    if kind == "healthy":
        return edges
    if kind == "duplicate_publisher":
        return tuple(
            replace(e, duplicate_publisher=True) if (e.parent, e.child) == ("map", "odom")
            else e for e in edges
        )
    if kind == "missing_static":
        return tuple(
            replace(e, absent=True) if (e.parent, e.child) == ("base_link", "laser")
            else e for e in edges
        )
    if kind == "stamped_ahead":
        return tuple(
            replace(e, stamp_offset_s=0.2) if (e.parent, e.child) == ("odom", "base_link")
            else e for e in edges
        )
    if kind == "intermittent_chain":
        return tuple(
            replace(e, gaps=((20.0, 26.0), (44.0, 51.0))) if (e.parent, e.child) == ("map", "odom")
            else e for e in edges
        )
    raise ValueError(f"unknown tf scenario {kind!r}")


#: scored scenarios. "healthy" must produce nothing; each other must be caught by name.
TF_SCENARIOS: tuple[str, ...] = (
    "healthy", "duplicate_publisher", "missing_static", "stamped_ahead",
    "intermittent_chain",
)

#: sensors whose stamps the transforms are supposed to align with. Extrapolation risk is
#: only meaningful against a consumer, so a TF fixture needs one.
TF_CONSUMERS: tuple[TopicSpec, ...] = (
    TopicSpec("/scan", "sensor_msgs/msg/LaserScan", 10.0, capture_delay_s=0.030),
    TopicSpec("/camera/image_raw", "sensor_msgs/msg/CompressedImage", 30.0,
              capture_delay_s=0.018),
)


def _tf_payload(rows: list[tuple[TfEdgeSpec, float, float]]) -> dict[str, Any]:
    """One TFMessage carrying every transform due at this instant."""
    return {
        "transforms": [
            {
                "header": _header(stamp_t, e.parent),
                "child_frame_id": e.child,
                "transform": {
                    "translation": {
                        "x": 0.5 * stamp_t + offset,
                        "y": 2.0 * math.sin(stamp_t / 10),
                        "z": 0.0,
                    },
                    "rotation": {"x": 0.0, "y": 0.0, "z": math.sin(stamp_t / 20),
                                 "w": math.cos(stamp_t / 20)},
                },
            }
            for e, stamp_t, offset in rows
        ]
    }


def _tf_messages(
    edges: tuple[TfEdgeSpec, ...], duration: float, rng: random.Random
) -> list[tuple[float, float, str, dict[str, Any]]]:
    """(log_t, pub_t, topic, payload) for `/tf` and `/tf_static`.

    Dynamic transforms are batched per publish instant the way a real `tf2` broadcaster
    batches them; static ones are latched once at the start, which is what makes an absent
    static transform so hard to spot — there is no rate to go missing.
    """
    out: list[tuple[float, float, str, dict[str, Any]]] = []

    statics = [e for e in edges if e.static and not e.absent]
    if statics:
        out.append((0.0, 0.0, "/tf_static",
                    _tf_payload([(e, 0.0, 0.0) for e in statics])))

    for e in edges:
        if e.static or e.absent or e.hz <= 0:
            continue
        for t in _schedule(e.hz, duration, rng, 0.02):
            if any(lo <= t < hi for lo, hi in e.gaps):
                continue
            out.append((t, t, "/tf", _tf_payload([(e, t + e.stamp_offset_s, 0.0)])))
            if e.duplicate_publisher:
                # a second broadcaster, a few milliseconds behind and disagreeing. Same
                # edge, same stamp: from a consumer's side this is one transform that
                # will not hold still.
                t2 = t + 0.004
                out.append((t2, t2, "/tf",
                            _tf_payload([(e, t + e.stamp_offset_s,
                                          e.duplicate_disagreement_m)])))

    out.sort(key=lambda r: r[0])
    return out


@dataclass
class StageSpec:
    """One stage of a stamp-propagating pipeline (F1).

    ``delay_s`` is the mean delay from the *upstream* publish to this stage's publish.
    For the first stage the upstream instant is the capture itself, so its delay is the
    sensor's own capture→publish cost.
    """

    topic: str
    msg_type: str
    delay_s: float
    delay_cv: float = 0.12
    #: False for a message type carrying no ``header`` — the age chain cannot be measured
    #: here, and the detector must report the stage unmeasurable rather than substitute
    #: the arrival time, which would silently invent an age
    stamped: bool = True
    #: False for a node that restamps with "now" instead of forwarding the capture time.
    #: That node has destroyed the trace, and saying so is itself a finding.
    propagates_stamp: bool = True


@dataclass
class PipelineSpec:
    """A chain of topics that pass one capture stamp along, stage by stage."""

    hz: float
    stages: tuple[StageSpec, ...]
    jitter_cv: float = 0.02


#: the worked example from NEWFEATURES F1: 12 ms capture→publish, 82 ms perception,
#: 37 ms planning, 131 ms end to end
DEFAULT_PIPELINE = PipelineSpec(
    hz=30.0,
    stages=(
        StageSpec("/camera/image_raw", "sensor_msgs/msg/CompressedImage", 0.012),
        StageSpec("/detections", "geometry_msgs/msg/PoseArray", 0.082),
        StageSpec("/cmd_vel_stamped", "geometry_msgs/msg/TwistStamped", 0.037),
    ),
)

#: the same chain, but the actuator publishes bare Twist. The last stage is unmeasurable
#: and must be reported as such — this is the fixture that guards the rule.
UNSTAMPED_PIPELINE = PipelineSpec(
    hz=30.0,
    stages=(
        StageSpec("/camera/image_raw", "sensor_msgs/msg/CompressedImage", 0.012),
        StageSpec("/detections", "geometry_msgs/msg/PoseArray", 0.082),
        StageSpec("/cmd_vel", "geometry_msgs/msg/Twist", 0.037, stamped=False),
    ),
)

#: a middle stage that restamps with "now", breaking the causal chain behind it
RESTAMPING_PIPELINE = PipelineSpec(
    hz=30.0,
    stages=(
        StageSpec("/camera/image_raw", "sensor_msgs/msg/CompressedImage", 0.012),
        StageSpec("/detections", "geometry_msgs/msg/PoseArray", 0.082,
                  propagates_stamp=False),
        StageSpec("/cmd_vel_stamped", "geometry_msgs/msg/TwistStamped", 0.037),
    ),
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


def stale_pipeline(topic: str, from_ms: float, to_ms: float, t_start: float,
                   ramp_duration: float) -> Fault:
    """Grow one pipeline stage's delay over a window (F1).

    The mean end-to-end age drifts, but the discriminating signal is the tail: P99 moves
    first and moves further. ``topic`` names the stage whose delay grows.
    """
    return Fault("stale_pipeline", topic=topic, t_start=t_start,
                 t_end=t_start + ramp_duration,
                 params={"from_ms": from_ms, "to_ms": to_ms}, detector="data_age")


def truncation(truncate_at_fraction: float) -> Fault:
    return Fault("truncation", params={"fraction": truncate_at_fraction}, detector="file_integrity")


def crc_corruption(at_fraction: float = 0.5, length: int = 64) -> Fault:
    """Flip a run of bytes inside the file, the way a bad disk or a bad cable does.

    Distinct from truncation: the file is complete and the summary is intact, so the
    damage only shows up when a chunk fails to decompress or its CRC does not match.
    """
    return Fault("crc_corruption", params={"fraction": at_fraction, "length": float(length)},
                 detector="file_integrity")


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
    to_log = _log_time_fn(faults)

    out: dict[str, list[tuple[float, float]]] = {}
    for tp, times in sched.items():
        out[tp] = [(t, to_log(t)) for t in times]
    return out


def _log_time_fn(faults: list[Fault]):
    """publish time → log time, given recorder lag (D6b) and clock steps (D6a/6c).

    Factored out because pipeline stages need the same mapping: a stage's publish time is
    derived from its upstream, but the recorder's clock applies to it identically.
    """
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

    def fn(t: float) -> float:
        log_t = t
        if lag_growth and t > lag_t0:
            log_t += lag_growth * (t - lag_t0)
        for st, delta in steps:
            if t >= st:
                log_t += delta
        return log_t

    return fn


def _pipeline_messages(
    pipelines: tuple[PipelineSpec, ...], duration: float, faults: list[Fault],
    rng: random.Random,
) -> list[tuple[float, float, str, float | None]]:
    """Emit (log_t, pub_t, topic, stamp_t) for every stage of every pipeline.

    One capture instant produces one message per stage, each published later than the
    last and each carrying the *capture* stamp — which is exactly what makes the age
    measurable downstream. ``stamp_t`` is None where the stage's type has no header.
    """
    ramps: dict[str, Any] = {}
    for f in faults:
        if f.kind == "stale_pipeline" and f.topic:
            def make(a=f.params["from_ms"] / 1000.0, b=f.params["to_ms"] / 1000.0,
                     t0=f.t_start, t1=f.t_end):
                def fn(t: float) -> float:
                    if t <= t0:
                        return a
                    if t >= t1:
                        return b
                    return a + (b - a) * (t - t0) / max(t1 - t0, 1e-9)

                return fn

            ramps[f.topic] = make()

    to_log = _log_time_fn(faults)
    out: list[tuple[float, float, str, float | None]] = []

    for pipe in pipelines:
        captures = _schedule(pipe.hz, duration, rng, pipe.jitter_cv)
        for t_capture in captures:
            prev_pub = t_capture
            stamp = t_capture
            for stage in pipe.stages:
                mean = stage.delay_s
                ramp = ramps.get(stage.topic)
                if ramp is not None:
                    mean = ramp(t_capture)
                if stage.delay_cv > 0 and mean > 0:
                    sigma = math.sqrt(math.log(1.0 + stage.delay_cv ** 2))
                    d = mean * math.exp(rng.gauss(0.0, sigma) - sigma * sigma / 2)
                else:
                    d = mean
                pub = prev_pub + d
                if not stage.propagates_stamp:
                    # the node restamps with "now": the trace back to capture is gone
                    stamp = pub
                out.append((to_log(pub), pub, stage.topic,
                            stamp if stage.stamped else None))
                prev_pub = pub

    out.sort(key=lambda r: r[0])
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


def _payload(msg_type: str, t: float, i: int, rng: random.Random,
             stamp_t: float | None = None) -> dict[str, Any]:
    """A payload that is cheap to build but has real, analysable numeric structure.

    ``t`` drives the *content*; ``stamp_t`` drives ``header.stamp``. They are the same
    for an ordinary sensor topic, and they differ for a pipeline stage, which republishes
    a derived result carrying the capture time of the data it came from. That difference
    is the whole of F1: the gap between the stamp and the publish time is the data's age.
    """
    st = t if stamp_t is None else stamp_t
    if msg_type == "sensor_msgs/msg/Imu":
        return {
            "header": _header(st, "imu_link"),
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
            "header": _header(st, "odom"),
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
            "header": _header(st, "laser"),
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
        return {"header": _header(st, "camera"), "format": "jpeg", "data": _tiny_jpeg()}
    if msg_type == "sensor_msgs/msg/Image":
        w = h = 16
        # a moving bright band, so consecutive frames actually differ
        band = int((t * 4) % h)
        pixels = bytes(
            (200 if row == band else 40) for row in range(h) for _ in range(w * 3)
        )
        return {
            "header": _header(st, "camera"),
            "height": h,
            "width": w,
            "encoding": "rgb8",
            "is_bigendian": 0,
            "step": w * 3,
            "data": pixels,
        }
    if msg_type == "sensor_msgs/msg/PointCloud2":
        n_points = 128
        point_step = 12  # x, y, z as float32
        import struct as _struct

        blob = b"".join(
            _struct.pack(
                "<fff",
                3.0 * math.cos(k / 20 + t),
                3.0 * math.sin(k / 20 + t),
                0.1 * ((k % 7) - 3),
            )
            for k in range(n_points)
        )
        return {
            "header": _header(st, "lidar"),
            "height": 1,
            "width": n_points,
            "fields": [
                {"name": "x", "offset": 0, "datatype": 7, "count": 1},
                {"name": "y", "offset": 4, "datatype": 7, "count": 1},
                {"name": "z", "offset": 8, "datatype": 7, "count": 1},
            ],
            "is_bigendian": False,
            "point_step": point_step,
            "row_step": point_step * n_points,
            "data": blob,
            "is_dense": True,
        }
    if msg_type == "nav_msgs/msg/Path":
        # a straight-line plan; /odom sines away from it, so deviation is measurable
        return {
            "header": _header(st, "map"),
            "poses": [
                {
                    "header": _header(st, "map"),
                    "pose": {
                        "position": {"x": 0.5 * (t + k), "y": 0.0, "z": 0.0},
                        "orientation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0},
                    },
                }
                for k in range(10)
            ],
        }
    if msg_type == "tf2_msgs/msg/TFMessage":
        return {
            "transforms": [
                {
                    "header": _header(st, "odom"),
                    "child_frame_id": "base_link",
                    "transform": {
                        "translation": {"x": 0.5 * t, "y": 2.0 * math.sin(t / 10), "z": 0.0},
                        "rotation": {"x": 0.0, "y": 0.0, "z": math.sin(t / 20), "w": math.cos(t / 20)},
                    },
                }
            ]
        }
    if msg_type == "geometry_msgs/msg/TwistStamped":
        return {
            "header": _header(st, "base_link"),
            "twist": {
                "linear": {"x": 0.5 + 0.1 * math.sin(t / 7), "y": 0.0, "z": 0.0},
                "angular": {"x": 0.0, "y": 0.0, "z": 0.2 * math.cos(t / 10)},
            },
        }
    if msg_type == "geometry_msgs/msg/PoseArray":
        return {
            "header": _header(st, "camera"),
            "poses": [
                {
                    "position": {"x": 2.0 + math.sin(t + k), "y": 0.3 * k, "z": 0.0},
                    "orientation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0},
                }
                for k in range((i % 3) + 1)
            ],
        }
    if msg_type == "rcl_interfaces/msg/Log":
        node, text = _LOG_LINES[i % len(_LOG_LINES)]
        return {
            "stamp": _stamp(st),
            "level": 20,
            "name": node,
            "msg": f"{text} (seq {i})",
            "file": "nav2.cpp",
            "function": "tick",
            "line": 42,
        }
    if msg_type == "diagnostic_msgs/msg/DiagnosticArray":
        return {
            "header": _header(st, ""),
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


def _qos_yaml(deadline_s: float | None, reliability: int = 1, depth: int = 10) -> str:
    if deadline_s is None and reliability == 1 and depth == 10:
        return ""
    sec = int(deadline_s or 0)
    nsec = int(((deadline_s or 0) - sec) * 1e9)
    return (
        f"- history: 1\n  depth: {depth}\n  reliability: {reliability}\n  durability: 2\n"
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
    pipelines: tuple[PipelineSpec, ...] = (),
    tf_edges: tuple[TfEdgeSpec, ...] = (),
) -> dict[str, Any]:
    """Write one MCAP with the given faults injected. Returns the ground truth dict."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    faults = list(faults or [])
    rng = random.Random(seed)

    # a topic driven by a pipeline is published by the pipeline alone: leaving it in the
    # plain schedule too would double its rate, which reads as a fault that was never
    # injected
    stage_topics = {st.topic for pipe in pipelines for st in pipe.stages}
    topics = tuple(s for s in topics if s.topic not in stage_topics)

    # `/tf` is produced here, not by a TopicSpec: it is many streams in one topic and its
    # payload is a batch, so it cannot come from `_payload(msg_type, t, i)`
    if tf_edges:
        topics = tuple(s for s in topics if s.topic not in ("/tf", "/tf_static"))

    schedules = _apply_faults(topics, duration_s, faults, rng)
    pipeline_msgs = _pipeline_messages(pipelines, duration_s, faults, rng)
    tf_msgs = _tf_messages(tf_edges, duration_s, rng)

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

        # pipeline stages are channels too; a stage may reuse a topic already declared
        # in `topics`, in which case the first declaration wins
        stage_specs = tuple(
            TopicSpec(st.topic, st.msg_type, pipe.hz, jitter_cv=pipe.jitter_cv)
            for pipe in pipelines
            for st in pipe.stages
        ) + tuple(
            TopicSpec(name, "tf2_msgs/msg/TFMessage", 0.0)
            for name in sorted({tp for _l, _p, tp, _d in tf_msgs})
        )
        declared: set[str] = set()
        all_specs: list[TopicSpec] = []
        for spec in (*topics, *stage_specs):
            if spec.topic not in declared:
                declared.add(spec.topic)
                all_specs.append(spec)

        for spec in all_specs:
            msgdef = MSGDEFS[spec.msg_type]
            if spec.msg_type not in schema_ids:
                schema_ids[spec.msg_type] = writer.register_schema(
                    spec.msg_type, "ros2msg", msgdef.encode()
                )
                encoders[spec.msg_type] = serialize_dynamic(spec.msg_type, msgdef)[spec.msg_type]
            meta = {}
            qos = _qos_yaml(spec.deadline_s, spec.reliability, spec.depth)
            if qos:
                meta["offered_qos_profiles"] = qos
            channel_ids[spec.topic] = writer.register_channel(
                topic=spec.topic,
                message_encoding="cdr",
                schema_id=schema_ids[spec.msg_type],
                metadata=meta,
            )

        type_of = {s.topic: s.msg_type for s in all_specs}
        seqs: dict[str, int] = dict.fromkeys(type_of, 0)

        # (log_t, pub_t, topic, stamp_t); stamp_t None means "stamp the publish time",
        # which is what an ordinary sensor topic does
        capture_delay = {s.topic: s.capture_delay_s for s in all_specs}
        # (log_t, pub_t, topic, stamp_t, explicit payload or None)
        merged: list[tuple[float, float, str, float | None, dict[str, Any] | None]] = [
            (log_t, pub_t, tp,
             pub_t - capture_delay[tp] if capture_delay.get(tp) else None, None)
            for tp, pairs in schedules.items()
            for pub_t, log_t in pairs
        ]
        merged.extend((lt, pt, tp, st, None) for lt, pt, tp, st in pipeline_msgs)
        merged.extend((lt, pt, tp, None, payload) for lt, pt, tp, payload in tf_msgs)
        merged.sort(key=lambda r: (r[0], r[2]))

        for log_t, pub_t, tp, stamp_t, explicit in merged:
            i = seqs[tp]
            seqs[tp] = i + 1
            data = encoders[type_of[tp]](
                explicit if explicit is not None
                else _payload(type_of[tp], pub_t, i, rng, stamp_t=stamp_t)
            )
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
        "pipelines": [
            {
                "hz": pipe.hz,
                "stages": [
                    {
                        "topic": st.topic,
                        "msg_type": st.msg_type,
                        # ground truth for F1: the delay this stage adds, and the
                        # cumulative age of the data it publishes
                        "delay_ms": st.delay_s * 1000.0,
                        "cumulative_age_ms": sum(
                            s2.delay_s for s2 in pipe.stages[: k + 1]
                        ) * 1000.0,
                        "stamped": st.stamped,
                        "propagates_stamp": st.propagates_stamp,
                    }
                    for k, st in enumerate(pipe.stages)
                ],
            }
            for pipe in pipelines
        ],
        "faults": [asdict(f) for f in faults],
        "clean": not faults,
    }

    # truncation and corruption are post-write byte operations, not schedule ones
    for f in faults:
        if f.kind == "truncation":
            raw = path.read_bytes()
            keep = max(1024, int(len(raw) * f.params["fraction"]))
            path.write_bytes(raw[:keep])
            truth["truncated_to_bytes"] = keep
            truth["original_bytes"] = len(raw)
        elif f.kind == "crc_corruption":
            raw = bytearray(path.read_bytes())
            # Corrupt inside a chunk's compressed payload, not wherever the fraction
            # happens to land: message-index records sit between chunks, and damaging
            # one of those is invisible because nothing reads them on a sequential scan.
            start = min(max(int(len(raw) * f.params["fraction"]), 1024), len(raw) - 1)
            try:
                from mcap.reader import make_reader

                with path.open("rb") as fh:
                    summary = make_reader(fh).get_summary()
                chunks = list(summary.chunk_indexes) if summary else []
                if chunks:
                    idx = min(int(len(chunks) * f.params["fraction"]), len(chunks) - 1)
                    chunk = chunks[idx]
                    start = int(chunk.chunk_start_offset) + max(64, int(chunk.chunk_length) // 2)
            except Exception:
                pass
            start = min(start, len(raw) - 1)
            length = min(int(f.params["length"]), len(raw) - start)
            for i in range(start, start + length):
                raw[i] ^= 0xFF
            path.write_bytes(bytes(raw))
            truth["corrupted_at_byte"] = start
            truth["corrupted_bytes"] = length

    gt = path.with_suffix(".ground_truth.json")
    gt.write_text(json.dumps(truth, indent=2))
    return truth


def to_db3(src_mcap: str | Path, dst_dir: str | Path) -> Path:
    """Convert a generated MCAP into a rosbag2 SQLite directory.

    Conversion rather than a second writer: it keeps one source of truth for the
    schedule, and it exercises the same file layout a real `ros2 bag record` produces,
    metadata.yaml included.
    """
    from rosbags.convert import convert

    dst = Path(dst_dir)
    if dst.exists():
        return next(dst.glob("*.db3"))
    convert(srcs=[Path(src_mcap)], dst=dst, dst_storage="sqlite3", dst_version=8,
            compress=None, compress_mode="none", default_typestore=None, typestore=None,
            exclude_topics=[], include_topics=[], exclude_msgtypes=[], include_msgtypes=[])
    return next(dst.glob("*.db3"))


def to_bag1(src_mcap: str | Path, dst: str | Path) -> Path:
    """Convert a generated MCAP into a ROS 1 `.bag`."""
    from rosbags.convert import convert

    target = Path(dst)
    if target.exists():
        return target
    convert(srcs=[Path(src_mcap)], dst=target, dst_storage="", dst_version=0,
            compress=None, compress_mode="none", default_typestore=None, typestore=None,
            exclude_topics=[], include_topics=[], exclude_msgtypes=[], include_msgtypes=[])
    return target


#: ULog file magic, followed by a one-byte format version. Same bytes pyulog verifies.
ULOG_MAGIC = b"\x55\x4c\x6f\x67\x01\x12\x35"


def ulog_name(topic: str) -> str:
    """The ULog dataset name a ROS topic maps to, and back again.

    ULog names a dataset after the uORB struct, so there is no room for a path: `/imu/data`
    becomes `imu_data`, which `UlogReader` then reports as `/imu_data`. Exported because
    the format-equivalence test needs the same mapping to line the four formats up.
    """
    return topic.lstrip("/").replace("/", "_")


def to_ulg(src_mcap: str | Path, dst: str | Path,
           dropouts: tuple[tuple[float, float], ...] = ()) -> Path:
    """Convert a generated MCAP into a PX4 `.ulg`.

    Written by hand because no converter exists in either direction, and because the
    format with the most *real* coverage in this repository had the least automated
    protection — a real flight is 70 MB, so CI carried none at all. This produces a few
    tens of KB from the same schedule the other three formats use, so a ULog-specific
    reader bug shows up in the equivalence test rather than in a re-run of the corpus.

    Only the subset of ULog that `UlogReader` reads is emitted: one dataset per topic,
    each `uint64 timestamp` plus one `float value`. `dropouts` are `(t_relative, duration_s)`
    pairs written as the logger's own dropout records — the same records that are the
    ground truth in `evals/integrity/real_data.py`.
    """
    import struct

    from baglens.readers import open_bag

    target = Path(dst)
    if target.exists():
        return target

    reader = open_bag(Path(src_mcap))
    try:
        arrivals = sorted(reader.arrivals(), key=lambda a: a.log_time_ns)
    finally:
        reader.close()
    if not arrivals:
        raise ValueError(f"{src_mcap} has no arrivals to convert")

    t0_ns = arrivals[0].log_time_ns
    ids: dict[str, int] = {}
    for a in arrivals:
        ids.setdefault(a.topic, len(ids))

    def msg(kind: bytes, payload: bytes) -> bytes:
        return struct.pack("<HB", len(payload), kind[0]) + payload

    out = bytearray()
    out += ULOG_MAGIC + bytes([1]) + struct.pack("<Q", t0_ns // 1000)
    # definitions section: flag bits, then one format per dataset
    out += msg(b"B", b"\x00" * 40)
    for topic in ids:
        out += msg(b"F", f"{ulog_name(topic)}:uint64_t timestamp;float value;".encode())
    # data section: subscriptions, then the samples themselves
    for topic, msg_id in ids.items():
        out += msg(b"A", struct.pack("<BH", 0, msg_id) + ulog_name(topic).encode())

    # Timestamps stay on the file's own clock rather than being rebased to zero: pyulog
    # dates a dropout record from the last data message it read, so the two must share a
    # scale or `truth_intervals` places every label outside the recording.
    pending = sorted(dropouts)
    t0_us = t0_ns // 1000
    for i, a in enumerate(arrivals):
        rel_us = (a.log_time_ns - t0_ns) // 1000
        out += msg(b"D", struct.pack("<HQf", ids[a.topic], t0_us + rel_us, float(i)))
        while pending and pending[0][0] <= rel_us / 1e6:
            _t, dur = pending.pop(0)
            out += msg(b"O", struct.pack("<H", int(dur * 1000)))

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(bytes(out))
    return target


def write_growing(path: str | Path, seed: int = 0, duration_s: float = 30.0,
                  topics: tuple[TopicSpec, ...] = SMALL_TOPICS) -> Path:
    """Write a bag and leave it *unfinished*: no summary, no trailing magic.

    This is what a recording looks like while it is still being written, and what the
    recovery path exists for. `Writer.finish()` is deliberately never called.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    schedules = _apply_faults(topics, duration_s, [], rng)

    with p.open("wb") as f:
        writer = Writer(f, compression=CompressionType.ZSTD, chunk_size=64 << 10)
        writer.start(profile="ros2", library="baglens-synth")
        schema_ids: dict[str, int] = {}
        channel_ids: dict[str, int] = {}
        encoders: dict[str, Any] = {}
        for spec in topics:
            msgdef = MSGDEFS[spec.msg_type]
            if spec.msg_type not in schema_ids:
                schema_ids[spec.msg_type] = writer.register_schema(
                    spec.msg_type, "ros2msg", msgdef.encode())
                encoders[spec.msg_type] = serialize_dynamic(spec.msg_type, msgdef)[spec.msg_type]
            channel_ids[spec.topic] = writer.register_channel(
                topic=spec.topic, message_encoding="cdr",
                schema_id=schema_ids[spec.msg_type], metadata={})

        type_of = {s.topic: s.msg_type for s in topics}
        merged = sorted(
            (log_t, pub_t, tp)
            for tp, pairs in schedules.items()
            for pub_t, log_t in pairs
        )
        for i, (log_t, pub_t, tp) in enumerate(merged):
            writer.add_message(
                channel_id=channel_ids[tp],
                log_time=BASE_EPOCH_NS + int(log_t * 1e9),
                publish_time=BASE_EPOCH_NS + int(pub_t * 1e9),
                data=encoders[type_of[tp]](_payload(type_of[tp], pub_t, i, rng)),
                sequence=i,
            )
        # no writer.finish(): the file stays open-ended, exactly as an interrupted
        # recorder leaves it
    return p


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
