"""Tools whose code paths no fixture previously reached: raw images, point clouds,
planned-path deviation, and log/signal coincidence."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from baglens.server import build_server


@pytest.fixture(scope="module")
def server() -> Any:
    return build_server()


def call(server: Any, name: str, args: dict[str, Any]) -> dict[str, Any]:
    result = asyncio.run(server.call_tool(name, args))
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text:
            return json.loads(text)
    return {}


# -- raw sensor_msgs/Image ---------------------------------------------------


def test_raw_image_frames_decode(server: Any, sensor_bag: Path) -> None:
    """Only CompressedImage was ever exercised; rgb8 unpacking had never run."""
    out = call(server, "frames.extract_keyframes",
               {"path": str(sensor_bag), "around_s": 30.0, "count": 3})
    assert out["frames"], out.get("note")
    for frame in out["frames"]:
        assert frame["width"] == 16 and frame["height"] == 16
        assert Path(frame["file"]).exists()


def test_raw_image_contact_sheet(server: Any, sensor_bag: Path) -> None:
    out = call(server, "frames.contact_sheet",
               {"path": str(sensor_bag), "start_s": 10, "end_s": 50, "count": 6})
    assert out["contact_sheet_file"]
    assert Path(out["contact_sheet_file"]).exists()
    assert len(out["frames"]) >= 3


# -- PointCloud2 -------------------------------------------------------------


def test_pointcloud_summary(server: Any, sensor_bag: Path) -> None:
    out = call(server, "pointcloud.summary", {"path": str(sensor_bag), "topic": "/points"})
    assert out["samples"] > 10
    assert out["mean_points"] == pytest.approx(128, rel=0.01)
    assert out["empty_return_ratio"] == 0.0
    assert "healthy" in out["verdict"]


def test_pointcloud_summary_on_a_missing_topic_is_graceful(server: Any, sensor_bag: Path) -> None:
    out = call(server, "pointcloud.summary", {"path": str(sensor_bag), "topic": "/nope"})
    assert out["samples"] == 0
    assert out["verdict"]


# -- trajectory deviation ----------------------------------------------------


def test_trajectory_deviation_against_a_real_plan(server: Any, sensor_bag: Path) -> None:
    """/odom sines away from a straight /plan, so the deviation is a known shape."""
    out = call(server, "spatial.trajectory_deviation",
               {"path": str(sensor_bag), "actual_topic": "/odom", "planned_topic": "/plan"})
    assert out["samples"] > 100
    assert out["max_deviation_m"] > 0.5
    assert out["mean_deviation_m"] > 0.0
    assert "deviation" in out["verdict"]


def test_trajectory_deviation_says_so_when_the_plan_is_missing(server: Any, clean_bag: Path) -> None:
    out = call(server, "spatial.trajectory_deviation",
               {"path": str(clean_bag), "actual_topic": "/odom", "planned_topic": "/plan"})
    assert "missing" in out["verdict"] or "empty" in out["verdict"]


def test_trajectory_summary_on_the_sensor_bag(server: Any, sensor_bag: Path) -> None:
    out = call(server, "spatial.trajectory_summary", {"path": str(sensor_bag), "topic": "/odom"})
    assert out["path_length_m"] > 10
    assert out["max_speed_ms"] > 0


# -- logs and signals --------------------------------------------------------


def test_log_signal_correlation_runs(server: Any, rich_bag: Path) -> None:
    out = call(server, "logs.correlate_with_signal",
               {"path": str(rich_bag), "topic": "/imu/data",
                "field_path": "linear_acceleration.z", "level": "ERROR"})
    assert "verdict" in out
    assert out["coincidence_rate"] >= 0.0


def test_log_signal_correlation_reports_when_nothing_matches(server: Any, rich_bag: Path) -> None:
    out = call(server, "logs.correlate_with_signal",
               {"path": str(rich_bag), "topic": "/imu/data",
                "field_path": "linear_acceleration.z", "level": "FATAL"})
    assert out["bursts"] == 0
    assert "no log lines" in out["verdict"]


def test_tf_report_on_the_rich_bag(server: Any, rich_bag: Path) -> None:
    out = call(server, "spatial.tf_report", {"path": str(rich_bag)})
    assert out["links"]
    assert out["roots"] == ["odom"]
    assert out["links"][0]["hz"] == pytest.approx(50, rel=0.05)
