"""MCP entrypoint. stdio by default, streamable-HTTP on request.

Read-only by construction: no writer code path exists in the core.
"""

from __future__ import annotations

import argparse
import importlib
import logging
import sys
from pathlib import Path

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

A verdict of `unassessable` is not a bad grade — it is a refusal to grade. Too little of
the recording could be measured, so its short finding list is NOT evidence that the
recording is healthy. Read `assessability.reasons`, tell the user what could not be
assessed, and do not draw conclusions from the absence of findings.
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


def _gate_main(argv: list[str]) -> int:
    """`baglens gate <dir>` — the training-data gate. See `baglens/gate.py`."""
    import json
    import multiprocessing as mp

    from .gate import GatePolicy, render, run_gate

    ap = argparse.ArgumentParser(
        prog="baglens gate",
        description="decide which episodes in a dataset are safe to train on",
    )
    ap.add_argument("path", help="an episode, or a directory of them")
    ap.add_argument("--out", type=Path, default=None,
                    help="write the JSON manifest here (default: stdout summary only)")
    ap.add_argument("--require", default="",
                    help="comma-separated topics every episode must contain; loss and "
                         "stall limits are applied to these rather than to all topics")
    ap.add_argument("--min-score", type=float, default=None)
    ap.add_argument("--min-duration", type=float, default=1.0)
    ap.add_argument("--max-stall-fraction", type=float, default=0.02)
    ap.add_argument("--max-drop-fraction", type=float, default=0.05)
    ap.add_argument("--max-gap", type=float, default=None,
                    help="longest single silence allowed on a required topic, in seconds. "
                         "Off by default because only you know how long an episode should "
                         "be; for 30 fps demonstration data, set it under a second")
    ap.add_argument("--max-lag", type=float, default=1.0,
                    help="how far the recorder may fall behind the publishers (seconds)")
    ap.add_argument("--accept-unassessable", action="store_true",
                    help="flag rather than reject episodes the auditor could not assess")
    ap.add_argument("--workers", type=int, default=max(1, (mp.cpu_count() or 2) - 1))
    ap.add_argument("--verbose", "-v", action="store_true",
                    help="print every rejection and its reasons")
    ap.add_argument("--strict", action="store_true",
                    help="exit non-zero unless every episode was accepted; without it, "
                         "only a rejection fails the run")
    ap.add_argument("--root", action="append", default=[], metavar="DIR")
    args = ap.parse_args(argv)

    set_config(load_config(roots=args.root or None))
    policy = GatePolicy(
        min_duration_s=args.min_duration,
        min_score=args.min_score,
        max_stall_fraction=args.max_stall_fraction,
        max_drop_fraction=args.max_drop_fraction,
        max_gap_s=args.max_gap,
        max_clock_lag_s=args.max_lag,
        require_topics=tuple(t for t in args.require.split(",") if t),
        reject_unassessable=not args.accept_unassessable,
    )
    manifest = run_gate(Path(args.path).expanduser(), policy, workers=args.workers)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(manifest, indent=2))
    print(render(manifest, verbose=args.verbose))
    if args.out:
        print(f"\nmanifest: {args.out}")

    summary = manifest["summary"]
    if not summary["episodes"]:
        print("no recordings found")
        return 1
    if summary["rejected"]:
        return 1
    return 1 if (args.strict and summary["review"]) else 0


#: subcommands that are not the MCP server. `baglens` with no subcommand still starts the
#: server over stdio, because that is what every MCP client config already invokes.
SUBCOMMANDS = {"gate": _gate_main}


def main(argv: list[str] | None = None) -> int:
    args_in = list(sys.argv[1:] if argv is None else argv)
    if args_in and args_in[0] in SUBCOMMANDS:
        return SUBCOMMANDS[args_in[0]](args_in[1:])

    ap = argparse.ArgumentParser(
        prog="baglens",
        description="MCP server for robot log analysis. Subcommands: gate",
    )
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
        # host and port are transport kwargs, not settings attributes: assigning them
        # to `mcp.settings` silently did nothing and --port was ignored
        mcp.run(transport="streamable-http", host=args.host, port=args.port)
    else:
        mcp.run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
