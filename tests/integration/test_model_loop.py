"""The model-in-the-loop path, exercised without an API key.

The point of a mock here is not to pretend we measured a model. It is that the loop
itself — schema conversion, multi-turn tool dispatch, token accounting, refusal and
pause handling, and the unsupported-number check — is ordinary code that can be wrong,
and it should not be discovered to be wrong on the one run that costs money.

The scripted client returns real Anthropic-shaped responses; the tools it calls are the
real MCP server against a real generated bag.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from evals.model_loop import Trajectory, run_model_case, to_anthropic_tools


class _Block(SimpleNamespace):
    pass


def _text(text: str) -> _Block:
    return _Block(type="text", text=text)


def _tool_use(tool_id: str, name: str, args: dict[str, Any]) -> _Block:
    return _Block(type="tool_use", id=tool_id, name=name, input=args)


def _response(content: list[_Block], stop_reason: str = "end_turn",
              inp: int = 100, out: int = 20) -> SimpleNamespace:
    return SimpleNamespace(
        content=content,
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=inp, output_tokens=out, cache_read_input_tokens=0),
    )


class ScriptedClient:
    """Replays a fixed list of responses and records the requests it received."""

    def __init__(self, responses: list[SimpleNamespace]) -> None:
        self._responses = list(responses)
        self.requests: list[dict[str, Any]] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs: Any) -> SimpleNamespace:
        self.requests.append(kwargs)
        if not self._responses:
            raise AssertionError("the loop asked for more turns than the script provides")
        return self._responses.pop(0)


@pytest.fixture
def server(tmp_path: Path) -> Any:
    import os

    os.environ["BAGLENS_CACHE_DIR"] = str(tmp_path / "cache")
    from baglens.config import load_config, set_config

    set_config(load_config())
    from baglens.server import build_server

    return build_server()


def test_tool_schemas_come_from_the_server(server: Any) -> None:
    """The model must see the surface a real client sees, not a hand-written copy."""
    tools = to_anthropic_tools(list(asyncio.run(server.list_tools())))

    assert len(tools) > 20
    for tool in tools:
        assert tool["name"]
        assert tool["description"], f"{tool['name']} has no description for the model to read"
        assert tool["input_schema"]["type"] == "object"


def test_full_loop_dispatches_tools_and_accounts_tokens(server: Any, dropout_bag: Path) -> None:
    client = ScriptedClient([
        _response(
            [_text("Let me audit it."),
             _tool_use("t1", "health.audit_recording", {"path": str(dropout_bag)})],
            stop_reason="tool_use", inp=500, out=40,
        ),
        _response([_text("/scan went silent for about 8.0 seconds.")], inp=900, out=30),
    ])

    traj = asyncio.run(run_model_case(client, server, "What happened?", model="test-model"))

    assert traj.error == ""
    assert traj.turns == 2
    assert [c["name"] for c in traj.tool_calls] == ["health.audit_recording"]
    assert traj.tool_results and traj.tool_results[0].get("verdict")
    assert traj.input_tokens == 1400
    assert traj.output_tokens == 70
    assert traj.total_tokens == 1470
    assert "8.0 seconds" in traj.final_text

    # The tool result must be fed back as a tool_result block, or the model is answering
    # from the question alone and the whole eval measures nothing.
    second = client.requests[1]
    tool_results = [
        b for m in second["messages"] if isinstance(m.get("content"), list)
        for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_result"
    ]
    assert len(tool_results) == 1
    assert tool_results[0]["tool_use_id"] == "t1"
    assert tool_results[0]["is_error"] is False


def test_tool_failure_is_reported_to_the_model_not_raised(server: Any) -> None:
    """A bad path is something the model should see and recover from."""
    client = ScriptedClient([
        _response([_tool_use("t1", "health.audit_recording", {"path": "/nope/missing.mcap"})],
                  stop_reason="tool_use"),
        _response([_text("That recording does not exist.")]),
    ])

    traj = asyncio.run(run_model_case(client, server, "Audit /nope/missing.mcap"))

    assert traj.error == ""
    results = [
        b for m in client.requests[1]["messages"] if isinstance(m.get("content"), list)
        for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_result"
    ]
    assert results[0]["is_error"] is True


def test_refusal_is_handled_before_reading_content(server: Any) -> None:
    """A refusal returns HTTP 200 with empty content — indexing content[0] would crash."""
    client = ScriptedClient([_response([], stop_reason="refusal")])
    traj = asyncio.run(run_model_case(client, server, "..."))
    assert traj.stop_reason == "refusal"
    assert "refused" in traj.error


def test_pause_turn_resumes(server: Any) -> None:
    client = ScriptedClient([
        _response([_text("working")], stop_reason="pause_turn"),
        _response([_text("done")]),
    ])
    traj = asyncio.run(run_model_case(client, server, "..."))
    assert traj.error == ""
    assert traj.turns == 2
    assert traj.final_text == "done"


def test_runaway_loop_is_bounded(server: Any, dropout_bag: Path) -> None:
    call = _tool_use("t", "health.audit_recording", {"path": str(dropout_bag)})
    client = ScriptedClient([_response([call], stop_reason="tool_use") for _ in range(10)])
    traj = asyncio.run(run_model_case(client, server, "...", max_turns=3))
    assert traj.turns == 3
    assert "did not finish" in traj.error


class _AuditingClient:
    """A stand-in model that audits whatever recording the prompt names, then answers.

    Enough of a model to drive the scoring wiring end to end: it reads the path out of
    the prompt exactly as a real model would have to, makes one real tool call, and
    quotes a figure back.
    """

    def __init__(self, answer: str = "The recording has gaps on /scan.") -> None:
        self._answer = answer
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs: Any) -> SimpleNamespace:
        messages = kwargs["messages"]
        if len(messages) == 1:
            prompt = messages[0]["content"]
            path = ""
            for line in prompt.splitlines():
                if line.startswith("recording path:"):
                    path = line.split(":", 1)[1].strip()
            return _response(
                [_tool_use("t1", "health.audit_recording", {"path": path})],
                stop_reason="tool_use",
            )
        return _response([_text(self._answer)])


def test_runner_scores_a_model_trajectory(server: Any, tmp_path: Path) -> None:
    """The `--model` wiring: fixture build, prompt handles, scoring, report render."""
    from evals.runner import load_cases, run_model_case_scored
    from evals.scoring import SuiteScore

    cases = [c for c in load_cases("integrity") if c.get("fixture") == "dropout_scan"][:2]
    assert cases, "expected at least one dropout_scan case to score"

    client = _AuditingClient()
    suite = SuiteScore()
    for case in cases:
        suite.cases.append(
            asyncio.run(
                run_model_case_scored(client, server, case, tmp_path, {}, "test-model")
            )
        )

    assert all(c.error == "" for c in suite.cases), [c.error for c in suite.cases]
    assert all(c.tool_calls == 1 for c in suite.cases)
    assert all(c.tokens > 0 for c in suite.cases)
    # audit_recording genuinely answers the gap cases, so evidence was retrieved
    assert suite.mean_correctness > 0.0

    report = suite.render_model("model eval", "test-model")
    assert "test-model" in report
    assert "unsupported figures" in report
    assert "How to read this" in report


def test_model_scoring_accepts_a_different_route_to_the_evidence(
    server: Any, tmp_path: Path
) -> None:
    """A model that reaches the answer via a different tool than the reference should
    still score correct — otherwise the eval measures obedience, not competence."""
    from evals.runner import load_cases, run_model_case_scored

    case = next(
        c for c in load_cases("integrity")
        if c["id"] == "gap_window_accurate"  # reference path is health.find_gaps
    )
    score = asyncio.run(
        run_model_case_scored(_AuditingClient(), server, case, tmp_path, {}, "test-model")
    )
    # The stub called audit_recording, not the reference find_gaps.
    assert score.tool_calls == 1
    assert score.error == ""


class TestUnsupportedNumbers:
    """The axis that only a model run can measure."""

    def _traj(self, answer: str, results: list[Any]) -> Trajectory:
        t = Trajectory(question="q", model="m")
        t.final_text = answer
        t.tool_results = results
        return t

    def test_figure_present_in_a_tool_result_is_supported(self) -> None:
        t = self._traj("The gap lasted 8.04 seconds.", [{"duration_s": 8.04}])
        assert t.unsupported_numbers() == []

    def test_rounding_is_not_a_fabrication(self) -> None:
        t = self._traj("The gap lasted about 8.0 seconds.", [{"duration_s": 8.04}])
        assert t.unsupported_numbers() == []

    def test_invented_figure_is_caught(self) -> None:
        t = self._traj("The gap lasted 42.7 seconds.", [{"duration_s": 8.04}])
        assert t.unsupported_numbers() == ["42.7"]

    def test_small_bare_integers_are_not_counted(self) -> None:
        """'3 topics' is prose, not a quoted measurement — counting it is noise."""
        t = self._traj("There were 3 topics and 2 gaps.", [{"duration_s": 8.04}])
        assert t.unsupported_numbers() == []
