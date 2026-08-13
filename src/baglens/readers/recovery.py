"""D8 / G4 — structural integrity and graceful degradation.

Never raise on a damaged file. This is where a real user's worst day intersects
with the tool.
"""

from __future__ import annotations

import struct
import time
from pathlib import Path

from ..models import ChunkIssue, FileIntegrity

MCAP_MAGIC = b"\x89MCAP0\r\n"
#: opcode of the Footer record; its presence at the tail means the summary was written
OP_FOOTER = 0x02
OP_CHUNK = 0x06
OP_MESSAGE = 0x05
OP_DATA_END = 0x0F


def _detect_format(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".mcap":
        return "mcap"
    if suffix in (".db3", ".sqlite3"):
        return "db3"
    if suffix == ".bag":
        return "bag1"
    if suffix in (".ulg", ".ulog"):
        return "ulog"
    return "unknown"


def _is_growing(path: Path, settle_s: float = 0.4) -> bool:
    """A file whose size changes while we look at it is still being recorded."""
    try:
        a = path.stat().st_size
        time.sleep(settle_s)
        b = path.stat().st_size
    except OSError:
        return False
    if b != a:
        return True
    return (time.time() - path.stat().st_mtime) < 2.0


def validate_file(path: str | Path) -> FileIntegrity:
    p = Path(path)
    fi = FileIntegrity(path=str(p), format=_detect_format(p))
    if not p.exists():
        fi.readable = False
        fi.score = 0.0
        fi.notes.append("file does not exist")
        return fi
    fi.size_bytes = p.stat().st_size
    fi.in_progress = _is_growing(p)

    if fi.format == "mcap":
        _validate_mcap(p, fi)
    elif fi.format == "db3":
        _validate_db3(p, fi)
    else:
        fi.notes.append(f"structural validation not implemented for {fi.format}; timing checks only")

    penalty = 0.0
    if not fi.has_summary:
        penalty += 15.0
    if fi.truncated_bytes:
        penalty += min(35.0, 10.0 + fi.truncated_bytes / max(fi.size_bytes, 1) * 100.0)
    penalty += min(40.0, 12.0 * len(fi.chunk_issues))
    if not fi.readable:
        penalty = 100.0
    fi.score = max(0.0, 100.0 - penalty)
    return fi


def _validate_mcap(p: Path, fi: FileIntegrity) -> None:
    from mcap.reader import make_reader

    from .mcap_reader import recover_messages

    with p.open("rb") as f:
        head = f.read(len(MCAP_MAGIC))
        if head != MCAP_MAGIC:
            fi.readable = False
            fi.notes.append("bad MCAP magic — not an MCAP file or the header is destroyed")
            return
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - len(MCAP_MAGIC)))
        if f.read(len(MCAP_MAGIC)) != MCAP_MAGIC:
            fi.has_summary = False
            fi.partial = True
            fi.notes.append("missing trailing magic — file truncated or still being written")

    summary = None
    try:
        with p.open("rb") as f:
            summary = make_reader(f).get_summary()
    except Exception as exc:
        fi.notes.append(f"summary unreadable: {type(exc).__name__}")

    if summary is None or summary.statistics is None:
        fi.has_summary = False
        fi.partial = True
        if "missing trailing magic" not in " ".join(fi.notes):
            fi.notes.append("no summary section — recovering by sequential chunk scan")
    else:
        fi.has_summary = True
        fi.last_readable_time = summary.statistics.message_end_time / 1e9

    # Sequential recovery pass: how far can we actually read?
    last_offset = 0
    last_time = 0
    n = 0
    for _schema, _channel, message, offset in recover_messages(p):
        n += 1
        last_time = max(last_time, message.log_time)
        last_offset = offset

    unreadable = max(0, fi.size_bytes - last_offset)
    if not fi.has_summary and n:
        fi.partial = True
        # a healthy file ends with a summary; bytes past the last recoverable record
        # in a file without one are lost, not merely unindexed
        if unreadable > 4096:
            fi.truncated_bytes = unreadable
            fi.chunk_issues.append(
                ChunkIssue(
                    kind="truncated",
                    offset=last_offset,
                    t_end=last_time / 1e9 if last_time else None,
                    detail="records end mid-stream",
                )
            )
            fi.notes.append(
                f"recovered {n} messages by sequential scan; "
                f"~{unreadable} bytes at the tail are unreadable"
            )
        else:
            fi.notes.append(f"recovered {n} messages by sequential scan")
    elif not n and not fi.has_summary:
        fi.readable = False
        fi.notes.append("no readable messages — the file is destroyed, not merely truncated")

    if last_time:
        fi.last_readable_time = last_time / 1e9
    if fi.in_progress:
        fi.notes.append("file is still growing — audit reflects what exists right now")


def _validate_db3(p: Path, fi: FileIntegrity) -> None:
    import sqlite3

    if not (p.parent / "metadata.yaml").exists():
        fi.partial = True
        fi.notes.append("no metadata.yaml — topic list reconstructed from the SQLite schema")
    try:
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        result = conn.execute("PRAGMA quick_check").fetchone()
        if result and result[0] != "ok":
            fi.chunk_issues.append(ChunkIssue(kind="unreadable", detail=str(result[0])))
        row = conn.execute("SELECT MAX(timestamp) FROM messages").fetchone()
        if row and row[0]:
            fi.last_readable_time = row[0] / 1e9
        conn.close()
    except Exception as exc:
        fi.readable = False
        fi.notes.append(f"sqlite unreadable: {exc}")


def truncate_copy(src: Path, dst: Path, fraction: float) -> Path:
    """Test helper: copy the first ``fraction`` of a file, producing a real truncation."""
    data = src.read_bytes()
    dst.write_bytes(data[: max(1, int(len(data) * fraction))])
    return dst


__all__ = ["validate_file", "truncate_copy", "OP_CHUNK", "OP_FOOTER", "OP_MESSAGE", "struct"]
