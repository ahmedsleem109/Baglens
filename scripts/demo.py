"""Record the README demo: a real finding, in a real flight, through the real tool surface.

This drives `server.call_tool` — the same entrypoint an MCP client uses — against a
public PX4 flight, and prints what the agent receives. It is a *scripted* sequence, not
a model run: nothing here decides what to call next, and no prose is generated. The
model-in-the-loop path is `evals/model_loop.py`, which needs an API key.

    uv run --extra ulog python scripts/demo.py ~/data/public/px4/588ff157-*.ulg

Recorded with `scripts/record_demo.sh`.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from baglens.server import build_server  # noqa: E402

DIM = "\033[2m"
BOLD = "\033[1m"
CYAN = "\033[36m"
RED = "\033[31m"
YELLOW = "\033[33m"
GREEN = "\033[32m"
OFF = "\033[0m"

SEV = {5: (RED, "CRITICAL"), 4: (RED, "CRITICAL"), 3: (YELLOW, "HIGH"), 2: (CYAN, "MEDIUM")}

#: the timeline is 120 rows on a PX4 flight; these five tell the story
DEMO_ROWS = (
    "/vehicle_attitude",
    "/sensor_combined",
    "/battery_status",
    "/vehicle_gps_position",
    "/actuator_outputs",
)


def say(text: str = "", pause: float = 0.0) -> None:
    print(text, flush=True)
    if pause:
        time.sleep(pause)


def tool_call(name: str, args: dict[str, Any]) -> None:
    shown = ", ".join(f"{k}={v!r}" for k, v in args.items())
    say(f"{DIM}  ▸{OFF} {CYAN}{name}{OFF}{DIM}({shown}){OFF}", 0.4)


async def call(server: Any, name: str, args: dict[str, Any]) -> Any:
    result = await server.call_tool(name, args)
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"text": text}
    return getattr(result, "structuredContent", None) or {}


async def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else ""
    if not path or not Path(path).exists():
        print("usage: demo.py <recording>", file=sys.stderr)
        return 2
    short = f"~/data/px4/{Path(path).name[:13]}….ulg"
    server = build_server()

    say()
    say(f"{BOLD}> Can I trust this flight log before I debug the mag failure?{OFF}", 1.2)
    say()

    # 1. always audit first -----------------------------------------------------
    tool_call("health.audit_recording", {"path": short})
    t = time.time()
    report = await call(server, "health.audit_recording", {"path": path})
    elapsed = time.time() - t

    verdict = report["verdict"]
    colour = GREEN if verdict == "trustworthy" else YELLOW
    say()
    say(f"    verdict: {colour}{BOLD}{verdict}{OFF}  (score {report['overall_score']}/100)")
    say(f"    {DIM}{report['duration_s']:.0f}s of flight, single pass, {elapsed:.1f}s{OFF}", 1.0)
    say()
    for f in report["findings"][:4]:
        col, label = SEV.get(f["severity"], (DIM, "INFO"))
        say(f"    {col}{label:<9}{OFF}{f['summary']}", 0.35)
    say()
    time.sleep(1.0)

    # 2. the distinction that matters: sensor, or recorder? ---------------------
    stall = next(f for f in report["findings"] if f["detector"] == "correlation")
    tool_call("health.explain_finding", {"finding_id": stall["id"]})
    detail = await call(server, "health.explain_finding", {"finding_id": stall["id"]})
    ev = detail.get("evidence", {})
    fin = detail.get("finding", stall)
    span = fin["t_end"] - fin["t_start"]
    say()
    say(f"    {BOLD}Not the magnetometer.{OFF} {int(ev.get('topics_silent', 0))} topics went silent")
    say(f"    together for {span:.2f}s — the recorder stalled, not a sensor.")
    say(
        f"    {DIM}{int(ev.get('gaps_rolled_up', 0))} per-topic silences inside this window are "
        f"one event, reported once.{OFF}",
        1.4,
    )
    say()

    # 3. show it -----------------------------------------------------------------
    tool_call("health.topic_timeline", {"path": short, "width": 72})
    tl = await call(server, "health.topic_timeline", {"path": path, "width": 72})
    say()
    for row in tl["rows"]:
        if row.split("|", 1)[0].strip() in DEMO_ROWS:
            say(f"    {row}", 0.25)
    say()
    say(f"    {DIM}{tl['legend']}{OFF}", 1.0)
    say()
    say(f"  {DIM}every number above carries its mission_id, time range and method.{OFF}", 2.0)
    say()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
