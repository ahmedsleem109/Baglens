"""Contract tests over the whole tool registry.

Written as a parametrised sweep on purpose: a new tool cannot be added without
complying, because the test discovers it automatically.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from baglens.budget import estimate_tokens
from baglens.config import CONFIG
from baglens.server import build_server

#: tools that legitimately return no provenance: they report on the server or the
#: filesystem rather than on data extracted from a recording
NO_PROVENANCE = {
    "catalog.add_source",
    "catalog.index_status",
    "catalog.tag_mission",
    "health.validate_file",
    "frames.extract_keyframes",
    "frames.contact_sheet",
}


@pytest.fixture(scope="module")
def server() -> Any:
    return build_server()


@pytest.fixture(scope="module")
def tools(server: Any) -> list[Any]:
    return asyncio.run(server.list_tools())


def call(server: Any, name: str, args: dict[str, Any]) -> dict[str, Any]:
    result = asyncio.run(server.call_tool(name, args))
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text:
            return json.loads(text)
    return {}


def test_every_tool_is_namespaced(tools: list[Any]) -> None:
    for tool in tools:
        assert "." in tool.name, f"{tool.name} is not namespace.verb_noun"
        namespace = tool.name.split(".")[0]
        assert namespace in {
            "health", "inspect", "timeseries", "catalog", "compare",
            "logs", "spatial", "frames", "pointcloud", "export",
        }


def test_every_tool_description_tells_the_model_when_to_use_it(tools: list[Any]) -> None:
    """Tool descriptions are prompts, not docstrings. Treat them as UX."""
    for tool in tools:
        desc = (tool.description or "").strip()
        assert len(desc) > 80, f"{tool.name} has a thin description"
        assert "\n" in desc, f"{tool.name} needs guidance beyond a one-liner"


def test_every_tool_has_a_typed_schema(tools: list[Any]) -> None:
    for tool in tools:
        schema = getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", None)
        assert isinstance(schema, dict), tool.name
        assert schema.get("type", "object") == "object", tool.name


@pytest.mark.parametrize(
    "name,args_key",
    [
        ("health.audit_recording", "path"),
        ("health.find_gaps", "path"),
        ("health.clock_report", "path"),
        ("health.topic_timeline", "path"),
        ("inspect.list_topics", "path"),
    ],
)
def test_results_carry_provenance(server: Any, clean_bag: Path, name: str, args_key: str) -> None:
    result = call(server, name, {args_key: str(clean_bag)})
    prov = result.get("provenance")
    assert prov, f"{name} returned no provenance"
    assert prov.get("method"), f"{name} provenance has no method"
    assert prov.get("path") or prov.get("mission_id")


def test_findings_carry_their_own_provenance(server: Any, stall_bag: Path) -> None:
    result = call(server, "health.audit_recording", {"path": str(stall_bag)})
    assert result["findings"], "expected findings on a stalled recording"
    for finding in result["findings"]:
        assert finding["provenance"]["method"], finding["summary"]
        assert finding["rule"], finding["summary"]
        assert finding["id"], finding["summary"]


@pytest.mark.parametrize(
    "name,args",
    [
        ("health.audit_recording", {}),
        ("health.find_gaps", {}),
        ("health.topic_timeline", {}),
        ("inspect.list_topics", {}),
        ("timeseries.extract", {"topic": "/imu/data", "field_path": "linear_acceleration.z",
                                "bin_s": 0.01}),
    ],
)
def test_no_tool_exceeds_its_budget(server: Any, stall_bag: Path, name: str,
                                    args: dict[str, Any]) -> None:
    result = call(server, name, {"path": str(stall_bag), **args})
    tokens = estimate_tokens(result)
    limit = CONFIG.budget.max_tokens
    assert tokens <= limit * 1.15, f"{name} returned ~{tokens} tokens against a {limit} budget"


def test_over_budget_results_say_how_to_narrow(server: Any, clean_bag: Path) -> None:
    """Truncation without guidance just makes the agent retry the same question."""
    result = call(
        server,
        "timeseries.extract",
        {"path": str(clean_bag), "topic": "/imu/data",
         "field_path": "linear_acceleration.z", "bin_s": 0.005},
    )
    if result.get("truncated"):
        assert result.get("suggested_narrowing")


def test_paths_outside_the_roots_are_refused(clean_bag: Path, tmp_path: Path) -> None:
    from dataclasses import replace

    from baglens.config import CONFIG as LIVE
    from baglens.config import set_config

    original = LIVE.current
    try:
        set_config(replace(original, roots=(tmp_path,)))
        from baglens.tools.common import resolve

        with pytest.raises(PermissionError):
            resolve(str(clean_bag))
    finally:
        set_config(original)


def test_damaged_file_never_raises_through_a_tool(server: Any, truncated_bag: Path) -> None:
    for name in ("health.validate_file", "health.audit_recording", "inspect.list_topics"):
        result = call(server, name, {"path": str(truncated_bag)})
        assert result, f"{name} returned nothing for a damaged file"


def test_missing_file_reports_rather_than_raises(server: Any, tmp_path: Path) -> None:
    result = call(server, "health.validate_file", {"path": str(tmp_path / "nope.mcap")})
    assert result["readable"] is False
