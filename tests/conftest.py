"""Fixtures. Bags are generated, never committed — CI regenerates them from a seed."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.synth import generate as g


@pytest.fixture(scope="session")
def bagdir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("bags")


@pytest.fixture(scope="session")
def clean_bag(bagdir: Path) -> Path:
    p = bagdir / "clean.mcap"
    g.generate_bag(p, seed=42, duration_s=90.0)
    return p


@pytest.fixture(scope="session")
def dropout_bag(bagdir: Path) -> Path:
    p = bagdir / "dropout.mcap"
    g.generate_bag(p, seed=1, duration_s=90.0, faults=[g.topic_dropout("/scan", 40.0, 8.0)])
    return p


@pytest.fixture(scope="session")
def degradation_bag(bagdir: Path) -> Path:
    p = bagdir / "degradation.mcap"
    g.generate_bag(
        p, seed=2, duration_s=180.0,
        faults=[g.rate_degradation("/odom", 50.0, 32.0, 20.0, 140.0)],
    )
    return p


@pytest.fixture(scope="session")
def jitter_bag(bagdir: Path) -> Path:
    p = bagdir / "jitter.mcap"
    g.generate_bag(p, seed=3, duration_s=120.0,
                   faults=[g.jitter_injection("/imu/data", 0.6, 40.0, 40.0)])
    return p


@pytest.fixture(scope="session")
def drops_bag(bagdir: Path) -> Path:
    p = bagdir / "drops.mcap"
    g.generate_bag(p, seed=4, duration_s=90.0, faults=[g.diffuse_drops("/camera/image_raw", 0.2)])
    return p


@pytest.fixture(scope="session")
def lag_bag(bagdir: Path) -> Path:
    p = bagdir / "lag.mcap"
    g.generate_bag(p, seed=5, duration_s=120.0, faults=[g.recorder_lag(150.0)])
    return p


@pytest.fixture(scope="session")
def step_bag(bagdir: Path) -> Path:
    p = bagdir / "step.mcap"
    g.generate_bag(p, seed=6, duration_s=90.0, faults=[g.clock_step(45.0, 1500.0, "forward")])
    return p


@pytest.fixture(scope="session")
def stall_bag(bagdir: Path) -> Path:
    p = bagdir / "stall.mcap"
    g.generate_bag(
        p, seed=7, duration_s=90.0,
        faults=[g.correlated_stall(("/imu/data", "/odom", "/scan", "/camera/image_raw"), 40.0, 6.0)],
    )
    return p


@pytest.fixture(scope="session")
def sensor_bag(bagdir: Path) -> Path:
    """Raw images, a point cloud and a planned path — the paths the small set skips."""
    p = bagdir / "sensors.mcap"
    g.generate_bag(p, seed=21, duration_s=60.0, topics=g.SENSOR_TOPICS)
    return p


@pytest.fixture(scope="session")
def rich_bag(bagdir: Path) -> Path:
    """The full topic set: logs, tf, diagnostics, camera."""
    p = bagdir / "rich.mcap"
    g.generate_bag(p, seed=22, duration_s=60.0, topics=g.DEFAULT_TOPICS)
    return p


@pytest.fixture(scope="session")
def corrupt_bag(bagdir: Path) -> Path:
    p = bagdir / "corrupt.mcap"
    g.generate_bag(p, seed=23, duration_s=60.0, faults=[g.crc_corruption(0.5)])
    return p


@pytest.fixture(scope="session")
def growing_bag(bagdir: Path) -> Path:
    """A recording that was never closed: no summary, no trailing magic."""
    return g.write_growing(bagdir / "growing.mcap", seed=24, duration_s=30.0)


@pytest.fixture(scope="session")
def db3_bag(clean_bag: Path, bagdir: Path) -> Path:
    return g.to_db3(clean_bag, bagdir / "clean_db3")


@pytest.fixture(scope="session")
def bag1_bag(clean_bag: Path, bagdir: Path) -> Path:
    return g.to_bag1(clean_bag, bagdir / "clean.bag")


@pytest.fixture(scope="session")
def ulg_bag(clean_bag: Path, bagdir: Path) -> Path:
    """The same clean recording as a PX4 `.ulg` — tens of KB, not the 70 MB a flight is."""
    pytest.importorskip("pyulog", reason="ULog reading needs the ulog extra")
    return g.to_ulg(clean_bag, bagdir / "clean.ulg")


@pytest.fixture(scope="session")
def ulg_dropout_bag(clean_bag: Path, bagdir: Path) -> Path:
    """Carries the logger's own dropout record — the label the real-data eval scores against."""
    pytest.importorskip("pyulog", reason="ULog reading needs the ulog extra")
    return g.to_ulg(clean_bag, bagdir / "dropout.ulg", dropouts=((40.0, 0.6), (41.0, 0.4)))


@pytest.fixture(scope="session")
def truncated_bag(bagdir: Path) -> Path:
    p = bagdir / "truncated.mcap"
    g.generate_bag(p, seed=8, duration_s=90.0, faults=[g.truncation(0.6)])
    return p
