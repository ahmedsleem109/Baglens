"""The schema gate is the whole safety property of the peek — test it hard.

If `stamp_offset` ever says "yes" for a schema with no header, F1 starts reporting data
ages computed from battery voltages, and they will look plausible.
"""

from __future__ import annotations

import struct

import pytest

from baglens.readers.stamp_peek import peek_stamp_ns, stamp_offset, write_stamp_ns
from tests.synth.msgdefs import MSGDEFS

HEADERED = [
    "sensor_msgs/msg/Imu",
    "nav_msgs/msg/Odometry",
    "sensor_msgs/msg/LaserScan",
    "sensor_msgs/msg/CompressedImage",
    "sensor_msgs/msg/Image",
    "sensor_msgs/msg/PointCloud2",
    "nav_msgs/msg/Path",
    "diagnostic_msgs/msg/DiagnosticArray",
    "geometry_msgs/msg/TwistStamped",
    "geometry_msgs/msg/PoseArray",
]

#: no `header` as the first serialised field, whatever else they may contain
UNHEADERED = [
    "geometry_msgs/msg/Twist",
    "std_msgs/msg/Float64",
    # a list of stamped transforms is not itself stamped: TF needs a real decode (F3)
    "tf2_msgs/msg/TFMessage",
]


@pytest.mark.parametrize("msg_type", HEADERED)
def test_headered_schemas_expose_a_stamp(msg_type: str) -> None:
    assert stamp_offset(MSGDEFS[msg_type]) == 4


@pytest.mark.parametrize("msg_type", UNHEADERED)
def test_unheadered_schemas_are_refused(msg_type: str) -> None:
    assert stamp_offset(MSGDEFS[msg_type]) is None


def test_a_leading_bare_time_counts_and_skips_constants() -> None:
    """`rcl_interfaces/msg/Log` declares eight constants before its stamp.

    Constants take no bytes on the wire, so the stamp is still at offset 4 — but only if
    the parser knows not to treat `byte DEBUG=10` as the first field.
    """
    assert stamp_offset(MSGDEFS["rcl_interfaces/msg/Log"]) == 4


def test_comments_and_blank_lines_do_not_become_fields() -> None:
    schema = "# a comment\n\n  # another\nstd_msgs/Header header\nfloat64 x\n"
    assert stamp_offset(schema) == 4


def test_a_float_first_field_is_refused() -> None:
    """The case that makes the gate necessary: these bytes unpack fine, and are garbage."""
    assert stamp_offset("float32 data\n") is None


def test_non_ros2_encodings_are_refused() -> None:
    assert stamp_offset(MSGDEFS["sensor_msgs/msg/Imu"], encoding="ros1msg") is None


def _cdr(sec: int, nanosec: int, little: bool = True) -> bytes:
    head = b"\x00\x01\x00\x00" if little else b"\x00\x00\x00\x00"
    body = struct.pack("<iI" if little else ">iI", sec, nanosec)
    return head + body + b"\xff" * 16


@pytest.mark.parametrize("little", [True, False])
def test_peek_reads_both_endiannesses(little: bool) -> None:
    assert peek_stamp_ns(_cdr(1780, 250_000_000, little)) == 1780 * 10**9 + 250_000_000


def test_peek_returns_zero_for_an_unset_stamp() -> None:
    """Zero is data, not absence: a node that never sets the stamp is a real fault, and
    the detector reports it as one."""
    assert peek_stamp_ns(_cdr(0, 0)) == 0


def test_peek_refuses_a_message_too_short_to_hold_a_stamp() -> None:
    assert peek_stamp_ns(b"\x00\x01\x00\x00\x05") is None


def test_peek_refuses_an_impossible_nanosecond_field() -> None:
    """A nanosecond field at or above 1e9 is not a stamp, whatever the schema said."""
    assert peek_stamp_ns(_cdr(1780, 1_000_000_000)) is None


def test_write_stamp_round_trips() -> None:
    raw = _cdr(1780, 250_000_000)
    want = 1234 * 10**9 + 5
    assert peek_stamp_ns(write_stamp_ns(raw, 4, want)) == want


def test_write_stamp_changes_only_the_stamp() -> None:
    raw = _cdr(1780, 250_000_000)
    out = write_stamp_ns(raw, 4, 99 * 10**9)
    assert len(out) == len(raw)
    assert out[:4] == raw[:4] and out[12:] == raw[12:]
