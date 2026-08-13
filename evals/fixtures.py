"""Named fixture recipes for the eval suite. Deterministic given a seed."""

from __future__ import annotations

from pathlib import Path

from tests.synth import generate as g

DURATION = 90.0

RECIPES: dict[str, dict] = {
    "clean": {"seed": 101, "faults": []},
    "dropout_scan": {"seed": 102, "faults": [g.topic_dropout("/scan", 40.0, 8.0)]},
    "system_stall": {
        "seed": 103,
        "faults": [g.correlated_stall(
            ("/imu/data", "/odom", "/scan", "/camera/image_raw"), 40.0, 6.0)],
    },
    "degrading_odom": {
        "seed": 104, "faults": [g.rate_degradation("/odom", 50.0, 32.0, 15.0, 70.0)]
    },
    "jittery_imu": {"seed": 105, "faults": [g.jitter_injection("/imu/data", 0.6, 35.0, 40.0)]},
    "dropping_camera": {"seed": 106, "faults": [g.diffuse_drops("/camera/image_raw", 0.2)]},
    "lagging_recorder": {"seed": 107, "faults": [g.recorder_lag(150.0)]},
    "stepping_clock": {"seed": 108, "faults": [g.clock_step(45.0, 1500.0, "forward")]},
    "truncated": {"seed": 109, "faults": [g.truncation(0.6)]},
}

#: fixtures that need the full topic set (logs, tf, diagnostics, camera)
RICH = {"rich"}

#: a small corpus for the catalog and compare cases
CORPUS = ("clean", "dropout_scan", "system_stall", "degrading_odom",
          "jittery_imu", "dropping_camera", "lagging_recorder", "stepping_clock")


def build(name: str, out_dir: Path) -> Path:
    """Generate (or reuse) the bag for a fixture name."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{name}.mcap"

    if name == "missing":
        return out_dir / "definitely_not_here.mcap"

    if path.exists():
        return path

    if name == "rich":
        g.generate_bag(path, seed=201, duration_s=DURATION, topics=g.DEFAULT_TOPICS, faults=[])
        return path

    if name == "qos_mismatch":
        # declare a 1 Hz deadline on a 100 Hz topic: a contract the topic does not honour
        topics = tuple(
            g.TopicSpec(t.topic, t.msg_type, t.hz, deadline_s=1.0 if t.topic == "/imu/data" else None)
            for t in g.SMALL_TOPICS
        )
        g.generate_bag(path, seed=202, duration_s=DURATION, topics=topics, faults=[])
        return path

    recipe = RECIPES.get(name)
    if recipe is None:
        raise KeyError(f"unknown fixture {name}")
    g.generate_bag(path, duration_s=DURATION, **recipe)  # type: ignore[arg-type]
    return path


def build_corpus(out_dir: Path) -> list[Path]:
    return [build(name, out_dir) for name in CORPUS]
