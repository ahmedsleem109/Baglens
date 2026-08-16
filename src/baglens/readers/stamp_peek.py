"""Reading ``header.stamp`` without deserializing the message.

The audit is payload-free by design, and that is why it sustains 44,500 msg/s. F1 needs
the capture time, which lives *in* the payload — so this module buys exactly one field
and nothing else.

**The claim, and the measurement behind it.** In ROS 2 CDR a message whose first field is
a ``std_msgs/Header`` puts ``sec`` (int32) and ``nanosec`` (uint32) immediately after the
4-byte encapsulation header, at offset 4, naturally aligned. Verified by
``scripts/verify_stamp_peek.py`` against a full decode on every headered topic of every
real recording in ``~/data/public/ros2``: 132 topics, zero disagreements. Re-run it before
trusting this on a new corpus.

**The gate is the schema, never the bytes.** ``std_msgs/Float32`` has no header, and its
first eight bytes unpack perfectly happily into a plausible-looking stamp. Deciding
per-message would invent data ages out of battery voltages. So the decision is made once
per schema, from the message definition, and a schema whose first field is not a Header
(or a bare Time) is never peeked at all — it is reported unmeasurable instead.
"""

from __future__ import annotations

import struct
from typing import Any

#: types whose serialised form begins with sec:int32, nanosec:uint32
_HEADER_TYPES = frozenset({
    "std_msgs/Header",
    "std_msgs/msg/Header",
    "Header",
})
_TIME_TYPES = frozenset({
    "builtin_interfaces/Time",
    "builtin_interfaces/msg/Time",
    "Time",
})

#: 4-byte CDR encapsulation header, then the first field
_STAMP_OFFSET = 4

_LE = struct.Struct("<iI")
_BE = struct.Struct(">iI")


def _first_field(schema_text: str) -> tuple[str, str] | None:
    """The first *serialised* field of the top-level message.

    Concatenated msgdefs put dependencies after a line of ``=`` signs, so the top-level
    definition is everything before the first separator. Constants (``byte DEBUG=10``)
    occupy no bytes on the wire and are skipped — ``rcl_interfaces/msg/Log`` declares
    eight of them before its stamp.
    """
    top = schema_text.split("=" * 20, 1)[0]
    for raw in top.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        # a constant, not a field: "uint8 LEVEL=10" or "uint8 LEVEL = 10"
        if "=" in line.split(None, 1)[1]:
            continue
        return parts[0], parts[1]
    return None


def stamp_offset(schema_text: str, encoding: str = "ros2msg") -> int | None:
    """Byte offset of ``sec`` in this schema's messages, or None if there is no stamp.

    None means "this topic's age is unmeasurable", which is a reportable answer — not a
    reason to fall back to arrival time.
    """
    if encoding not in ("ros2msg", "ros2idl", ""):
        return None
    field = _first_field(schema_text or "")
    if field is None:
        return None
    ftype, fname = field
    if ftype in _HEADER_TYPES and fname == "header":
        return _STAMP_OFFSET
    # a message that leads with a bare Time (rcl_interfaces/msg/Log) carries its stamp in
    # the same place, because Header's own first field is that Time
    if ftype in _TIME_TYPES and fname == "stamp":
        return _STAMP_OFFSET
    return None


def stamp_offset_for_type(msg_type: Any) -> int | None:
    """The same gate, for a live subscription, from an rclpy message class.

    A live `raw=True` subscription hands over the identical CDR buffer a recorder would
    have written, so the peek is identical — only the way the schema is discovered
    differs. Keeping both routes in this module is what stops the file path and the live
    path drifting into two different answers about the same bytes.
    """
    try:
        fields = msg_type.get_fields_and_field_types()
    except Exception:
        return None
    for name, ftype in fields.items():  # insertion-ordered: declaration order
        base = ftype.split("<", 1)[0]
        if base in _HEADER_TYPES and name == "header":
            return _STAMP_OFFSET
        if base in _TIME_TYPES and name == "stamp":
            return _STAMP_OFFSET
        return None
    return None


def peek_stamp_ns(data: bytes, offset: int = _STAMP_OFFSET) -> int | None:
    """Nanoseconds since epoch from a stamp at ``offset``, without decoding the message.

    Returns None for a message too short to hold one. A zero stamp is returned as 0 and
    not treated as missing: a node that never sets the stamp is a real fault, and the
    detector reports it as one.
    """
    if len(data) < offset + 8:
        return None
    little = bool(data[1] & 1)  # encapsulation: 0x0000 CDR_BE, 0x0001 CDR_LE
    sec, nanosec = (_LE if little else _BE).unpack_from(data, offset)
    if nanosec >= 1_000_000_000:
        # not a stamp we can believe; the schema said there was one, so say nothing
        # rather than emit a wrong age
        return None
    return sec * 1_000_000_000 + nanosec


def peek_frame_id(data: bytes, offset: int = _STAMP_OFFSET, max_len: int = 128) -> str | None:
    """The `frame_id` that follows the stamp, still without decoding the message.

    In a `std_msgs/Header` the string comes straight after the two time fields, as a
    4-byte length and then that many bytes including a NUL. F3 needs it to answer a
    question nothing else can: which coordinate frames does this sensor claim to be in,
    and does the transform tree actually connect them? A frame a sensor publishes in but
    that no transform ever provides is a static transform nobody launched.

    Returns None rather than guessing when the length is implausible — the schema gate
    says a Header is here, but a truncated or unexpected message must not become a frame.
    """
    start = offset + 8
    if len(data) < start + 4:
        return None
    little = bool(data[1] & 1)
    (n,) = struct.unpack_from("<I" if little else ">I", data, start)
    if n == 0 or n > max_len or len(data) < start + 4 + n:
        return None
    raw = data[start + 4 : start + 4 + n]
    return raw.split(b"\x00", 1)[0].decode("ascii", "replace") or None


def write_stamp_ns(data: bytes, offset: int, stamp_ns: int) -> bytes:
    """``data`` with its stamp replaced. Used only by the fault injector.

    The same fixed offset, written instead of read: this is how a stale-pipeline fault is
    injected into a *real* recording without re-encoding it. Everything else about the
    message — its bytes, its size, its arrival time — is left exactly as the robot
    recorded it, so the only thing the detector can be reacting to is the age.
    """
    if len(data) < offset + 8:
        return data
    if not 0 <= stamp_ns // 1_000_000_000 <= 2_147_483_647:
        # a stamp outside what the int32 seconds field can hold. Real recordings contain
        # topics stamped from a steady clock starting near zero, and ageing one of those
        # underflows; leave the message alone rather than write a nonsense stamp.
        return data
    little = bool(data[1] & 1)
    out = bytearray(data)
    (_LE if little else _BE).pack_into(
        out, offset, stamp_ns // 1_000_000_000, stamp_ns % 1_000_000_000
    )
    return bytes(out)
