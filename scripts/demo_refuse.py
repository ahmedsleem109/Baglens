"""Record the refusal demo: the tool declining to grade a recording it cannot measure.

Two recordings, side by side, through the same tool surface an MCP client uses. One is a
healthy 31-minute drive and gets a verdict. The other is an autonomous shuttle bus that
spent its entire recording parked — 70 of its 110 topics event-driven, none of them with a
measurable rate — and gets `unassessable` with reasons instead of a score.

The comparison is the whole point. A refusal only means something if the same tool
confidently grades the recording next to it; a tool that refuses everything is not
cautious, it is useless.

    uv run python scripts/demo_refuse.py ~/data/public/ros2/nuway_waypoints.mcap \
                                         ~/data/public/ros2/nuway_stops.mcap

Recorded with `scripts/record_demo.sh --refuse`.
"""

from __future__ import annotations

import asyncio
import json
import sys
import textwrap
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

COLOUR = {
    "trustworthy": GREEN,
    "usable_with_caveats": YELLOW,
    "compromised": RED,
    "unassessable": CYAN,
}


def say(text: str = "", pause: float = 0.0) -> None:
    print(text, flush=True)
    if pause:
        time.sleep(pause)


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


async def audit(server: Any, path: str, blurb: str) -> None:
    short = f"~/data/ros2/{Path(path).name}"
    say(f"{DIM}  ▸{OFF} {CYAN}health.audit_recording{OFF}{DIM}(path={short!r}){OFF}", 0.4)
    say(f"    {DIM}{blurb}{OFF}")
    report = await call(server, "health.audit_recording", {"path": path})

    verdict = report["verdict"]
    colour = COLOUR.get(verdict, DIM)
    assess = report.get("assessability") or {}
    say()
    if verdict == "unassessable":
        say(f"    verdict: {colour}{BOLD}{verdict}{OFF}"
            f"   {DIM}(confidence {assess.get('confidence', 0):.2f} — not a score){OFF}")
    else:
        say(f"    verdict: {colour}{BOLD}{verdict}{OFF}  (score {report['overall_score']}/100)")
    say(f"    {DIM}{report['duration_s']:.0f}s, {assess.get('topics_assessable', 0)} of "
        f"{assess.get('topics_total', 0)} topics measurable{OFF}", 1.0)
    say()
    for reason in assess.get("reasons", []):
        for i, line in enumerate(textwrap.wrap(reason, 96)):
            say(f"    {RED if i == 0 else ' '}{'· ' if i == 0 else '  '}{OFF}{line}", 0.2)
    say(pause=1.2)


async def main() -> int:
    paths = sys.argv[1:3]
    if len(paths) != 2 or not all(Path(p).exists() for p in paths):
        print("usage: demo_refuse.py <healthy recording> <unassessable recording>",
              file=sys.stderr)
        return 2
    server = build_server()

    say()
    say(f"{BOLD}> Audit both of these before I compare the two routes.{OFF}", 1.2)
    say()
    await audit(server, paths[0], "a shuttle bus driving a waypoint route")
    say()
    await audit(server, paths[1], "the same bus, parked, on a stops route")
    say()
    say(f"  {BOLD}It did not grade the second one.{OFF}")
    say(f"  {DIM}Not a bad score — a refusal. Nothing in that recording could be measured,{OFF}")
    say(f"  {DIM}so a short finding list there is not evidence of a healthy recorder.{OFF}", 2.2)
    say()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
