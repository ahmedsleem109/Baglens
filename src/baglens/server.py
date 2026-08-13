"""MCP entrypoint. stdio by default, streamable-HTTP on request.

Read-only by construction: no writer code path exists in the core.
"""

from __future__ import annotations

import argparse
import importlib
import logging
import sys

from mcp.server.mcpserver import MCPServer

from . import __version__
from .config import load_config, set_config

log = logging.getLogger("baglens")

INSTRUCTIONS = """\
baglens investigates robot recordings (ROS 2 .mcap/.db3, ROS 1 .bag, PX4 .ulg) and
fleets of them. Tools return statistics with provenance, never raw payloads.

Recommended order of operations:
  1. health.audit_recording  — ALWAYS first. It tells you whether the data can support
     the analysis you are about to do, and returns explicit caveats.
  2. inspect.* / timeseries.* — single-mission drill-down.
  3. catalog.* / compare.*    — corpus-level questions ("has this happened before?").
  4. export.report            — write up the finding with citations.

Every response is token-budgeted. If a result says truncated=true, read
suggested_narrowing and ask a narrower question rather than retrying the same one.
"""

#: tool namespace modules, registered in this order
TOOL_MODULES = (
    "baglens.tools.health_tools",
    "baglens.tools.inspect_tools",
    "baglens.tools.timeseries_tools",
    "baglens.tools.catalog_tools",
    "baglens.tools.compare_tools",
    "baglens.tools.logs_tools",
    "baglens.tools.spatial_tools",
    "baglens.tools.frames_tools",
    "baglens.tools.export_tools",
)


def build_server(modules: tuple[str, ...] = TOOL_MODULES) -> MCPServer:
    mcp = MCPServer(
        name="baglens",
        title="baglens — robot log investigator",
        version=__version__,
        instructions=INSTRUCTIONS,
    )
    for mod_name in modules:
        try:
            mod = importlib.import_module(mod_name)
        except ModuleNotFoundError as exc:  # namespace not built yet
            log.debug("skipping %s: %s", mod_name, exc)
            continue
        mod.register(mcp)
    return mcp


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="baglens", description="MCP server for robot log analysis")
    ap.add_argument("--stdio", action="store_true", help="run over stdio (default)")
    ap.add_argument("--http", action="store_true", help="run over streamable HTTP")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument(
        "--root",
        action="append",
        default=[],
        metavar="DIR",
        help="confine all reads under DIR (repeatable)",
    )
    ap.add_argument("--sensitivity", choices=["low", "normal", "high"], default=None)
    ap.add_argument("--no-frames", action="store_true", help="disable all image extraction")
    ap.add_argument("--log-level", default="WARNING")
    args = ap.parse_args(argv)

    logging.basicConfig(level=args.log_level.upper(), stream=sys.stderr)
    set_config(
        load_config(
            roots=args.root or None,
            sensitivity=args.sensitivity,
            allow_frames=not args.no_frames,
        )
    )

    mcp = build_server()
    if args.http:
        mcp.settings.host = args.host  # type: ignore[attr-defined]
        mcp.settings.port = args.port  # type: ignore[attr-defined]
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
