"""Record the training-data gate demo: which episodes are safe to train on, and why not.

Run against the injected corpus, because that is the only set here where the answers are
*known*: every copy is a real ROS 2 recording, and the faulted ones carry a fault someone
put there on purpose. A gate demo on unlabelled data would only be showing you its own
opinion.

    uv run python scripts/demo_gate.py ~/data/injected

Recorded with `scripts/record_demo.sh --gate`.
"""

from __future__ import annotations

import json
import sys
import textwrap
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from baglens.gate import GatePolicy, run_gate  # noqa: E402

DIM = "\033[2m"
BOLD = "\033[1m"
CYAN = "\033[36m"
RED = "\033[31m"
YELLOW = "\033[33m"
GREEN = "\033[32m"
OFF = "\033[0m"


def say(text: str = "", pause: float = 0.0) -> None:
    print(text, flush=True)
    if pause:
        time.sleep(pause)


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "").expanduser()
    if not root.exists():
        print("usage: demo_gate.py <directory of episodes>", file=sys.stderr)
        return 2

    out = root.parent / "manifest.json"

    say()
    say(f"{BOLD}$ baglens gate ~/data/episodes --out manifest.json --max-gap 0.5{OFF}", 1.0)
    say()
    say(f"  {DIM}auditing every episode — single pass each, bounded state{OFF}", 0.6)

    policy = GatePolicy(max_gap_s=0.5)
    manifest = run_gate(root, policy, workers=4)
    out.write_text(json.dumps(manifest, indent=2))
    summary = manifest["summary"]

    say()
    say(f"  {summary['episodes']} episodes")
    say(f"    {GREEN}accept {summary['accepted']}{OFF}   "
        f"{YELLOW}review {summary['review']}{OFF}   "
        f"{RED}reject {summary['rejected']}{OFF}")
    say(f"    {DIM}{summary['accepted_seconds']:.0f}s safe to train on, "
        f"{summary['rejected_seconds']:.0f}s withheld{OFF}", 1.2)
    say()
    say(f"  {BOLD}why each rejection was rejected{OFF}")
    for code, n in summary["rejections_by_code"].items():
        say(f"    {RED}{n:>4}{OFF}  {code}", 0.3)
    say(pause=1.2)

    say()
    say(f"  {DIM}a reason is not a score — here are three, verbatim from the manifest:{OFF}")
    say()
    shown = 0
    seen: set[str] = set()
    for episode in manifest["episodes"]:
        if episode["decision"] == "accept" or shown >= 3:
            continue
        reason = episode["reasons"][0]
        if reason["code"] in seen:
            continue
        seen.add(reason["code"])
        shown += 1
        say(f"    {RED}reject{OFF} {Path(episode['path']).name}")
        for line in textwrap.wrap(reason["detail"], 94)[:2]:
            say(f"      {DIM}{line}{OFF}", 0.25)
    say(pause=1.2)

    say()
    say(f"  {CYAN}manifest.json{OFF}{DIM} carries the decision, the reasons and the policy it ran"
        f" under,{OFF}")
    say(f"  {DIM}plus a `train_on` list your training job reads directly.{OFF}", 2.2)
    say()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
