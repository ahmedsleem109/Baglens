"""Transports, background indexing, pagination and confinement — through the real
tool surface rather than through the functions underneath it.

Each of these shipped once without ever having been called the way a client calls it.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest

from baglens.server import TOOL_MODULES, build_server, main


def call(server: Any, name: str, args: dict[str, Any]) -> dict[str, Any]:
    result = asyncio.run(server.call_tool(name, args))
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text:
            return json.loads(text)
    return {}


@pytest.fixture(scope="module")
def server() -> Any:
    return build_server()


# -- server construction and transports --------------------------------------


def test_server_registers_every_namespace(server: Any) -> None:
    tools = asyncio.run(server.list_tools())
    namespaces = {t.name.split(".")[0] for t in tools}
    assert namespaces == {
        "health", "inspect", "timeseries", "catalog", "compare",
        "logs", "spatial", "frames", "pointcloud", "export",
    }
    assert len(tools) >= 42


def test_server_builds_with_a_missing_namespace() -> None:
    """A namespace that fails to import must not take the server down with it."""
    partial = build_server((*TOOL_MODULES, "baglens.tools.does_not_exist"))
    assert asyncio.run(partial.list_tools())


def test_stdio_is_the_default_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    used: dict[str, Any] = {}

    def fake_run(self: Any, transport: str = "stdio", **kwargs: Any) -> None:
        used["transport"] = transport

    monkeypatch.setattr("mcp.server.mcpserver.MCPServer.run", fake_run)
    assert main([]) == 0
    assert used["transport"] == "stdio"


def test_http_transport_is_selectable(monkeypatch: pytest.MonkeyPatch) -> None:
    used: dict[str, Any] = {}

    def fake_run(self: Any, transport: str = "stdio", **kwargs: Any) -> None:
        used["transport"] = transport
        used.update(kwargs)

    monkeypatch.setattr("mcp.server.mcpserver.MCPServer.run", fake_run)
    assert main(["--http", "--port", "8799", "--host", "0.0.0.0"]) == 0
    assert used["transport"] == "streamable-http"
    # host and port must actually reach the transport, not a settings object nobody reads
    assert used["port"] == 8799
    assert used["host"] == "0.0.0.0"


def test_cli_flags_reach_the_active_config(monkeypatch: pytest.MonkeyPatch,
                                           tmp_path: Path) -> None:
    monkeypatch.setattr("mcp.server.mcpserver.MCPServer.run", lambda *a, **k: None)
    from baglens.config import CONFIG, set_config

    original = CONFIG.current
    try:
        main(["--root", str(tmp_path), "--sensitivity", "high", "--no-frames"])
        assert CONFIG.sensitivity == "high"
        assert CONFIG.allow_frames is False
        assert tmp_path in CONFIG.roots
    finally:
        set_config(original)


# -- confinement through a live tool call ------------------------------------


def test_a_tool_refuses_a_path_outside_the_roots(server: Any, clean_bag: Path,
                                                 tmp_path: Path) -> None:
    from dataclasses import replace

    from baglens.config import CONFIG, set_config

    original = CONFIG.current
    try:
        set_config(replace(original, roots=(tmp_path,)))
        with pytest.raises(Exception, match="outside the configured"):
            call(server, "inspect.list_topics", {"path": str(clean_bag)})
    finally:
        set_config(original)


def test_frames_are_disabled_when_configured(server: Any, sensor_bag: Path) -> None:
    from dataclasses import replace

    from baglens.config import CONFIG, set_config

    original = CONFIG.current
    try:
        set_config(replace(original, allow_frames=False))
        out = call(server, "frames.contact_sheet", {"path": str(sensor_bag)})
        assert out["frames"] == []
        assert "disabled" in out["note"]
    finally:
        set_config(original)


# -- background indexing -----------------------------------------------------


def test_background_indexing_reports_progress_and_finishes(server: Any, bagdir: Path,
                                                           clean_bag: Path, stall_bag: Path,
                                                           dropout_bag: Path, jitter_bag: Path,
                                                           tmp_path: Path,
                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BAGLENS_CACHE_DIR", str(tmp_path / "cache"))
    from baglens.config import CONFIG, load_config, set_config
    from baglens.tools import catalog_tools

    original = CONFIG.current
    try:
        set_config(load_config())
        catalog_tools._CATALOG = None
        started = call(server, "catalog.add_source",
                       {"path": str(bagdir), "background": True, "with_signals": False})
        assert started["indexing_started"] is True
        assert started["files_found"] >= 3

        deadline = time.time() + 120
        status = call(server, "catalog.index_status", {})
        assert status["total"] >= 3, "status must reflect the run that just started"
        while status["running"] and time.time() < deadline:
            time.sleep(0.5)
            status = call(server, "catalog.index_status", {})

        assert not status["running"], "indexing did not finish within the deadline"
        assert status["done"] == status["total"]
        assert status["progress"] == 1.0

        summary = call(server, "catalog.fleet_summary", {})
        assert summary["missions"] >= 3
    finally:
        set_config(original)
        catalog_tools._CATALOG = None


# -- pagination round trips --------------------------------------------------


def test_gap_continuation_token_walks_the_list(server: Any, stall_bag: Path) -> None:
    first = call(server, "health.find_gaps", {"path": str(stall_bag)})
    assert first["gaps"]
    if not first.get("continuation_token"):
        pytest.skip("this recording's gaps fit in one page")
    second = call(server, "health.find_gaps",
                  {"path": str(stall_bag), "continuation_token": first["continuation_token"]})
    assert second["offset"] == len(first["gaps"])
    assert second["gaps"][0] != first["gaps"][0]


def test_log_continuation_token_walks_the_list(server: Any, rich_bag: Path) -> None:
    first = call(server, "logs.query", {"path": str(rich_bag), "limit": 5})
    assert len(first["lines"]) == 5
    token = first.get("continuation_token")
    assert token, "a truncated log query must offer a way to continue"
    second = call(server, "logs.query",
                  {"path": str(rich_bag), "limit": 5, "continuation_token": token})
    assert second["offset"] == 5
    assert second["lines"][0]["t"] > first["lines"][0]["t"]


def test_mission_continuation_token_pages(server: Any) -> None:
    first = call(server, "catalog.list_missions", {"limit": 2})
    token = first.get("continuation_token")
    if not token:
        pytest.skip("catalog too small to paginate")
    second = call(server, "catalog.list_missions", {"continuation_token": token})
    assert second["missions"] != first["missions"]
