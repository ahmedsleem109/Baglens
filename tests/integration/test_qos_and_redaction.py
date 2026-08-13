"""`health.qos_report` and the redaction posture.

QoS is where silent loss is configured rather than caused, and redaction is the part of
the privacy posture that has to hold at the tool boundary — a field that reaches the
model has already left the building.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from baglens.config import CONFIG, set_config
from baglens.server import build_server
from tests.synth import generate as g


@pytest.fixture(scope="module")
def server() -> Any:
    return build_server()


@pytest.fixture(scope="module")
def lossy_qos_bag(bagdir: Path) -> Path:
    p = bagdir / "lossy_qos.mcap"
    g.generate_bag(p, seed=31, duration_s=60.0, topics=g.LOSSY_QOS_TOPICS)
    return p


def call(server: Any, name: str, args: dict[str, Any]) -> dict[str, Any]:
    result = asyncio.run(server.call_tool(name, args))
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text:
            return json.loads(text)
    return {}


# -- QoS ---------------------------------------------------------------------


def test_qos_report_reads_recorded_profiles(server: Any, lossy_qos_bag: Path) -> None:
    out = call(server, "health.qos_report", {"path": str(lossy_qos_bag)})
    by_topic = {t["topic"]: t for t in out["topics"]}
    assert by_topic["/imu/data"]["reliability"] == "best_effort"
    assert by_topic["/scan"]["depth"] == 1
    assert by_topic["/odom"]["declared_hz"] == pytest.approx(1.0)


def test_qos_report_flags_best_effort(server: Any, lossy_qos_bag: Path) -> None:
    out = call(server, "health.qos_report", {"path": str(lossy_qos_bag)})
    kinds = {(i["kind"], i["topic"]) for i in out["issues"]}
    assert ("best_effort", "/imu/data") in kinds
    detail = next(i for i in out["issues"] if i["kind"] == "best_effort")
    assert "drop" in detail["recommendation"]


def test_qos_report_flags_a_shallow_queue_on_a_fast_topic(server: Any,
                                                          lossy_qos_bag: Path) -> None:
    out = call(server, "health.qos_report", {"path": str(lossy_qos_bag)})
    kinds = {(i["kind"], i["topic"]) for i in out["issues"]}
    assert ("shallow_queue", "/camera/image_raw") in kinds
    # 10 Hz behind a depth-1 queue is not the same risk and must not be flagged
    assert ("shallow_queue", "/scan") not in kinds


def test_qos_report_flags_a_deadline_nobody_honours(server: Any, lossy_qos_bag: Path) -> None:
    out = call(server, "health.qos_report", {"path": str(lossy_qos_bag)})
    mismatch = next(i for i in out["issues"] if i["kind"] == "deadline_mismatch")
    assert mismatch["topic"] == "/odom"
    assert "1.0 Hz" in mismatch["detail"] and "50" in mismatch["detail"]


def test_qos_report_is_quiet_on_a_healthy_recording(server: Any, clean_bag: Path) -> None:
    out = call(server, "health.qos_report", {"path": str(clean_bag)})
    assert out["issues"] == []
    assert "no QoS profile" in out["verdict"]


def test_qos_report_says_when_nothing_was_recorded(server: Any, db3_bag: Path) -> None:
    out = call(server, "health.qos_report", {"path": str(db3_bag)})
    assert out["topics_without_qos"]
    assert out["verdict"]


# -- redaction ---------------------------------------------------------------


def test_redacted_topic_never_appears_in_a_report(server: Any, stall_bag: Path) -> None:
    original = CONFIG.current
    try:
        set_config(replace(original, redact_topics=("/camera/*",)))
        out = call(server, "health.audit_recording", {"path": str(stall_bag)})
        assert all(not t["topic"].startswith("/camera/") for t in out["topics"])
        assert all(
            f["topic"] is None or not f["topic"].startswith("/camera/") for f in out["findings"]
        )
    finally:
        set_config(original)


def test_redacted_field_is_masked_in_payloads(server: Any, clean_bag: Path) -> None:
    original = CONFIG.current
    try:
        set_config(replace(original, redact_fields=("/odom:position",)))
        out = call(server, "inspect.sample_messages",
                   {"path": str(clean_bag), "topic": "/odom", "count": 1})
        pose = out["samples"][0]["data"]["pose"]["pose"]
        assert pose["position"] == "<redacted>"
        assert pose["orientation"]["z"] != "<redacted>", "only the named field is masked"
    finally:
        set_config(original)


def test_redacted_field_cannot_be_read_through_statistics(server: Any, clean_bag: Path) -> None:
    """Masking the payload but serving the same numbers via field_stats would be theatre."""
    original = CONFIG.current
    try:
        set_config(replace(original, redact_fields=("/odom:position",)))
        out = call(server, "inspect.field_stats",
                   {"path": str(clean_bag), "topic": "/odom",
                    "field_path": "pose.pose.position.x"})
        assert out["stats"] == {}
        assert any("redacted" in w for w in out["provenance"]["warnings"])
    finally:
        set_config(original)


def test_redacted_field_cannot_be_read_through_timeseries(server: Any, clean_bag: Path) -> None:
    original = CONFIG.current
    try:
        set_config(replace(original, redact_fields=("/odom:position",)))
        out = call(server, "timeseries.extract",
                   {"path": str(clean_bag), "topic": "/odom",
                    "field_path": "pose.pose.position.x", "bin_s": 5.0})
        assert out["values"] == []
        assert out["stats"] == {}
    finally:
        set_config(original)


def test_untargeted_rule_applies_to_every_topic(server: Any, clean_bag: Path) -> None:
    original = CONFIG.current
    try:
        set_config(replace(original, redact_fields=("linear",)))
        out = call(server, "inspect.sample_messages",
                   {"path": str(clean_bag), "topic": "/cmd_vel", "count": 1})
        assert out["samples"][0]["data"]["linear"] == "<redacted>"
    finally:
        set_config(original)


def test_no_redaction_configured_changes_nothing(server: Any, clean_bag: Path) -> None:
    out = call(server, "inspect.sample_messages",
               {"path": str(clean_bag), "topic": "/odom", "count": 1})
    assert out["samples"][0]["data"]["pose"]["pose"]["position"]["x"] is not None
