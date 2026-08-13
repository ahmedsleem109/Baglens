"""Golden-file snapshots: catch unintended behaviour drift.

The eval suite checks assertions ("a gap was found"); these check *shape* — the exact
fields, the exact numbers, the exact wording an agent will read. A refactor that quietly
renames a field, reorders findings, or shifts a threshold passes every assertion-based
test and fails here, which is the point.

Regenerate deliberately, and read the diff before committing it:

    BAGLENS_UPDATE_GOLDEN=1 uv run pytest tests/integration/test_golden.py
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any

import pytest

from baglens.server import build_server

GOLDEN_DIR = Path(__file__).resolve().parents[1] / "golden"
UPDATE = os.environ.get("BAGLENS_UPDATE_GOLDEN") == "1"

#: keys whose values depend on where the file happens to live or how long the run took
VOLATILE = {"path", "mission_id", "file", "parquet_path", "elapsed_s", "indexed_at",
            "started_at", "size_bytes", "continuation_token"}


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


def normalise(node: Any) -> Any:
    """Strip everything that legitimately differs between two runs of the same code."""
    if isinstance(node, dict):
        out = {}
        for key, value in node.items():
            if key in VOLATILE:
                out[key] = "<volatile>"
            else:
                out[key] = normalise(value)
        return out
    if isinstance(node, list):
        return [normalise(v) for v in node]
    if isinstance(node, float):
        # detector arithmetic is deterministic, but float formatting across platforms
        # is not worth defending to the last bit
        return round(node, 3)
    if isinstance(node, str):
        # summaries embed the recording's name and absolute times
        node = re.sub(r"/[\w./-]+\.(mcap|db3|bag)", "<path>", node)
        return re.sub(r"\b1\.78\d{8,}\b", "<epoch>", node)
    return node


def check_golden(name: str, payload: Any) -> None:
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    target = GOLDEN_DIR / f"{name}.json"
    normalised = normalise(payload)
    if UPDATE or not target.exists():
        target.write_text(json.dumps(normalised, indent=2, sort_keys=True) + "\n")
        if not UPDATE:
            pytest.skip(f"created missing golden file {target.name}; re-run to compare")
        return
    expected = json.loads(target.read_text())
    assert normalised == expected, (
        f"{name} drifted from its snapshot. If the change is intended, review the diff "
        f"then regenerate with BAGLENS_UPDATE_GOLDEN=1."
    )


def test_golden_audit_clean(server: Any, clean_bag: Path) -> None:
    check_golden("audit_clean", call(server, "health.audit_recording", {"path": str(clean_bag)}))


def test_golden_audit_stall(server: Any, stall_bag: Path) -> None:
    check_golden("audit_stall", call(server, "health.audit_recording", {"path": str(stall_bag)}))


def test_golden_audit_dropout(server: Any, dropout_bag: Path) -> None:
    check_golden("audit_dropout",
                 call(server, "health.audit_recording", {"path": str(dropout_bag)}))


def test_golden_find_gaps(server: Any, stall_bag: Path) -> None:
    check_golden("find_gaps_stall", call(server, "health.find_gaps", {"path": str(stall_bag)}))


def test_golden_clock_report(server: Any, lag_bag: Path) -> None:
    check_golden("clock_report_lag", call(server, "health.clock_report", {"path": str(lag_bag)}))


def test_golden_timeline(server: Any, stall_bag: Path) -> None:
    check_golden("timeline_stall",
                 call(server, "health.topic_timeline", {"path": str(stall_bag), "width": 60}))


def test_golden_list_topics(server: Any, clean_bag: Path) -> None:
    check_golden("list_topics", call(server, "inspect.list_topics", {"path": str(clean_bag)}))


def test_golden_field_stats(server: Any, clean_bag: Path) -> None:
    check_golden(
        "field_stats",
        call(server, "inspect.field_stats",
             {"path": str(clean_bag), "topic": "/odom", "field_path": "twist.twist.linear.x"}),
    )


def test_golden_timeseries_extract(server: Any, dropout_bag: Path) -> None:
    check_golden(
        "timeseries_extract_gaps",
        call(server, "timeseries.extract",
             {"path": str(dropout_bag), "topic": "/scan", "field_path": "range_max",
              "bin_s": 5.0}),
    )


def test_golden_validate_truncated(server: Any, truncated_bag: Path) -> None:
    check_golden("validate_truncated",
                 call(server, "health.validate_file", {"path": str(truncated_bag)}))
