"""Does the fixed-offset stamp peek agree with a full decode? Check, do not assume.

F1 rests on one claim: in ROS 2 CDR, a message whose first field is a `std_msgs/Header`
puts `sec`/`nanosec` at offset 4, so reading `header.stamp` is an 8-byte peek rather than
a deserialization. This script is the harness behind that claim — it decodes every message
properly and compares, per topic, against what `stamp_peek` returns.

A published number with no script behind it is a guess, so the number in `docs/` comes
from here. Re-run it against any new corpus before trusting the peek on it:

    uv run python scripts/verify_stamp_peek.py ~/data/public/ros2
    uv run python scripts/verify_stamp_peek.py ~/data/public/ros2 --limit 500

Exit code is non-zero if any topic disagrees, so it can be wired into CI where a corpus
is available.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from baglens.readers.stamp_peek import peek_stamp_ns, stamp_offset


def _stamp_of(decoded: Any) -> tuple[int, int] | None:
    """The stamp a full decode says this message carries, or None if it has none."""
    hdr = getattr(decoded, "header", None)
    if hdr is not None and hasattr(hdr, "stamp"):
        return int(hdr.stamp.sec), int(hdr.stamp.nanosec)
    # a message that leads with a bare Time rather than a Header
    stamp = getattr(decoded, "stamp", None)
    if stamp is not None and hasattr(stamp, "sec"):
        return int(stamp.sec), int(stamp.nanosec)
    return None


def check_file(path: Path, limit: int) -> tuple[int, int, list[str]]:
    """(topics agreeing, topics disagreeing, report lines) for one recording."""
    from mcap.reader import make_reader
    from mcap_ros2.decoder import DecoderFactory

    agree: dict[str, int] = defaultdict(int)
    disagree: dict[str, int] = defaultdict(int)
    skipped: dict[str, str] = {}
    seen: dict[str, int] = defaultdict(int)
    examples: dict[str, str] = {}

    factory = DecoderFactory()
    decoders: dict[int, Any] = {}

    with path.open("rb") as f:
        for schema, channel, message in make_reader(f).iter_messages():
            topic = channel.topic
            if channel.message_encoding != "cdr" or schema is None:
                skipped[topic] = f"encoding={channel.message_encoding}"
                continue
            if seen[topic] >= limit:
                continue
            seen[topic] += 1

            schema_text = schema.data.decode("utf-8", "replace")
            offset = stamp_offset(schema_text, schema.encoding or "ros2msg")

            dec = decoders.get(schema.schema_id if hasattr(schema, "schema_id") else schema.id)
            if dec is None:
                try:
                    dec = factory.decoder_for("cdr", schema)
                except Exception:
                    dec = None
                if dec is None:
                    skipped[topic] = f"no decoder ({schema.name})"
                    continue
                decoders[schema.id] = dec

            truth = _stamp_of(dec(message.data))
            if offset is None:
                # the gate said "no stamp here". It is wrong only if a decode finds one.
                if truth is None:
                    skipped[topic] = f"no stamp ({schema.name})"
                else:
                    disagree[topic] += 1
                    examples.setdefault(
                        topic, f"gate said no stamp, decode found {truth} ({schema.name})"
                    )
                continue

            got = peek_stamp_ns(message.data, offset)
            want = None if truth is None else truth[0] * 1_000_000_000 + truth[1]
            if got == want:
                agree[topic] += 1
            else:
                disagree[topic] += 1
                examples.setdefault(topic, f"peek={got} decode={want} ({schema.name})")

    lines = [f"=== {path.name} ==="]
    for topic in sorted(set(agree) | set(disagree)):
        a, d = agree[topic], disagree[topic]
        lines.append(f"  {'OK  ' if d == 0 else 'FAIL'} {topic:<52} agree={a:<6} disagree={d}")
        if d:
            lines.append(f"        {examples[topic]}")
    for topic, why in sorted(skipped.items()):
        lines.append(f"  --   {topic:<52} {why}")
    return len(agree), len([t for t in disagree if disagree[t]]), lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("corpus", type=Path, help="directory of .mcap recordings, or one file")
    ap.add_argument("--limit", type=int, default=200,
                    help="messages checked per topic (default 200)")
    ap.add_argument("--quiet", action="store_true", help="summary only")
    args = ap.parse_args(argv)

    root: Path = args.corpus
    files = [root] if root.is_file() else sorted(root.glob("*.mcap"))
    if not files:
        print(f"no .mcap recordings under {root}", file=sys.stderr)
        return 2

    total_ok = total_bad = 0
    for path in files:
        ok, bad, lines = check_file(path, args.limit)
        total_ok += ok
        total_bad += bad
        if not args.quiet:
            print("\n".join(lines))

    print(
        f"\n{len(files)} recording(s): {total_ok} topics agree, {total_bad} disagree "
        f"(<= {args.limit} messages per topic)"
    )
    if total_bad:
        print("the fixed-offset peek is NOT safe on this corpus", file=sys.stderr)
    return 1 if total_bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
