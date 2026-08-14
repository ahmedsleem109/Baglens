"""Model-in-the-loop evaluation: hand the question and the real tool surface to a model.

The deterministic runner scores a *reference* tool sequence — it proves the tools work
and that their answers are citable. It cannot tell you whether a model can actually find
its way around this surface, which is the number that makes the project legible to
someone building agents rather than flying robots.

This module runs the real agentic loop against the live MCP server and scores the
trajectory on the same four axes as the deterministic path, plus one only a model run
can measure: **unsupported numbers** — figures in the model's prose that appear in no
tool result it received. The deterministic harness scores provenance coverage of tool
*output*; this scores whether the model's *answer* stayed anchored to it.

No API key, no run: this path is opt-in and never gates CI.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

DEFAULT_MODEL = "claude-opus-5"
DEFAULT_MAX_TOKENS = 16000

SYSTEM_PROMPT = """\
You are a robotics data analyst investigating recordings with the baglens tools.

Answer the user's question by calling tools. Ground every factual claim in a tool
result — cite the numbers the tools return rather than estimating, and do not state a
figure you did not read from a tool. If the tools cannot answer the question, say so
plainly instead of guessing.

Be concise. When you have the answer, state it directly."""

# Bare integers are too noisy to attribute (indices, counts, "the 3 topics"), so the
# check looks at decimals and large integers — the shapes an analyst would actually
# quote back: rates, durations, scores, message counts.
_NUMBER = re.compile(r"\d+\.\d+|\d{3,}")


class MessagesClient(Protocol):
    """The slice of the Anthropic client this module uses.

    Narrow on purpose: the mock in `tests/integration/test_model_loop.py` implements
    exactly this, so the loop, the scoring and the token accounting are all exercised
    in CI without an API key or a single network call.
    """

    @property
    def messages(self) -> Any: ...


@dataclass
class Trajectory:
    """Everything one model run did, and what it cost."""

    question: str
    model: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_results: list[Any] = field(default_factory=list)
    final_text: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    turns: int = 0
    wall_seconds: float = 0.0
    stop_reason: str = ""
    error: str = ""

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def unsupported_numbers(self) -> list[str]:
        """Numbers in the answer that appear in no tool result.

        Not a hallucination oracle — a model can round 12.47 to 12.5 legitimately, and
        this counts that. It is a *floor* on how anchored the prose is, and it is
        reported as such rather than dressed up as a correctness metric.
        """
        if not self.final_text:
            return []
        haystack = json.dumps(self.tool_results, default=str)
        out = []
        for token in _NUMBER.findall(self.final_text):
            if token in haystack:
                continue
            # A rounded restatement of a returned number is not a fabrication.
            try:
                value = float(token)
            except ValueError:
                continue
            if any(
                abs(value - float(m)) <= max(0.05 * abs(value), 0.5)
                for m in _NUMBER.findall(haystack)
                if _safe_float(m) is not None
            ):
                continue
            out.append(token)
        return out


def _safe_float(text: str) -> float | None:
    try:
        return float(text)
    except ValueError:
        return None


def to_anthropic_tools(mcp_tools: list[Any]) -> list[dict[str, Any]]:
    """Convert the MCP server's own tool list into Anthropic tool definitions.

    Deliberately no hand-written schema: the model sees exactly the surface a real
    client sees, so a bad description or a confusing schema shows up in the score
    instead of being papered over here.
    """
    out: list[dict[str, Any]] = []
    for tool in mcp_tools:
        schema = getattr(tool, "inputSchema", None) or getattr(tool, "input_schema", None)
        out.append(
            {
                "name": tool.name,
                "description": (getattr(tool, "description", "") or "").strip(),
                "input_schema": schema or {"type": "object", "properties": {}},
            }
        )
    return out


async def run_model_case(
    client: MessagesClient,
    server: Any,
    question: str,
    *,
    model: str = DEFAULT_MODEL,
    max_turns: int = 12,
    effort: str = "high",
    tool_filter: list[str] | None = None,
) -> Trajectory:
    """Drive one question through the real tool surface and record what happened."""
    from evals.runner import call as call_tool

    traj = Trajectory(question=question, model=model)
    started = time.perf_counter()

    mcp_tools = await server.list_tools()
    if tool_filter:
        wanted = set(tool_filter)
        mcp_tools = [t for t in mcp_tools if t.name in wanted]
    tools = to_anthropic_tools(list(mcp_tools))

    messages: list[dict[str, Any]] = [{"role": "user", "content": question}]

    for _ in range(max_turns):
        traj.turns += 1
        try:
            response = client.messages.create(
                model=model,
                max_tokens=DEFAULT_MAX_TOKENS,
                system=SYSTEM_PROMPT,
                thinking={"type": "adaptive"},
                output_config={"effort": effort},
                tools=tools,
                messages=messages,
            )
        except Exception as exc:  # noqa: BLE001 - one bad case must not lose the suite
            traj.error = f"{type(exc).__name__}: {exc}"
            break

        usage = getattr(response, "usage", None)
        if usage is not None:
            traj.input_tokens += int(getattr(usage, "input_tokens", 0) or 0)
            traj.output_tokens += int(getattr(usage, "output_tokens", 0) or 0)
            traj.cache_read_tokens += int(getattr(usage, "cache_read_input_tokens", 0) or 0)

        traj.stop_reason = str(getattr(response, "stop_reason", "") or "")

        # A refused request returns HTTP 200 with an empty or partial content list, so
        # reading content[0] unconditionally would break here rather than surface it.
        if traj.stop_reason == "refusal":
            traj.error = "model refused the request"
            break

        content = list(getattr(response, "content", []) or [])
        messages.append({"role": "assistant", "content": content})

        # A server-side tool hit its iteration cap; re-send to let it resume.
        if traj.stop_reason == "pause_turn":
            continue

        tool_uses = [b for b in content if getattr(b, "type", None) == "tool_use"]
        for block in content:
            if getattr(block, "type", None) == "text":
                traj.final_text = getattr(block, "text", "") or traj.final_text

        if not tool_uses:
            break

        results: list[dict[str, Any]] = []
        for block in tool_uses:
            name = block.name
            args = dict(block.input or {})
            traj.tool_calls.append({"name": name, "args": args})
            try:
                result = await call_tool(server, name, args)
                is_error = False
            except Exception as exc:  # noqa: BLE001 - the model should see the failure
                result = {"error": f"{type(exc).__name__}: {exc}"}
                is_error = True
            traj.tool_results.append(result)
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result, default=str)[:100_000],
                    "is_error": is_error,
                }
            )
        messages.append({"role": "user", "content": results})
    else:
        traj.error = traj.error or f"did not finish within {max_turns} turns"

    traj.wall_seconds = time.perf_counter() - started
    return traj


def build_client(api_key: str | None = None) -> MessagesClient:
    """Real Anthropic client. Imported lazily so the deterministic path never needs it."""
    import anthropic

    return anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
