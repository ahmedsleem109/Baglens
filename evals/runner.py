"""Eval runner.

Two modes:

* **deterministic** (the default, and the one CI runs) executes each case's reference
  tool sequence and scores the assertions, the token cost and the citation rate. No API
  calls, no network, fully reproducible.
* **model** (`--model`) hands the question and the real MCP tool surface to an LLM and
  scores what it actually does. That is the interesting number for comparing models, but
  it costs money and cannot run in CI, so it is opt-in.

    uv run python -m evals.runner --out evals/RESULTS.md
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any

import yaml

from evals import fixtures
from evals.scoring import CaseScore, SuiteScore, check, count_claims, estimate_tokens

CASE_DIR = Path(__file__).parent / "cases"
PLACEHOLDER = re.compile(r"\{([^}]+)\}")


def load_cases(only: str | None = None) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for f in sorted(CASE_DIR.glob("*.yaml")):
        if only and only not in f.stem:
            continue
        for case in yaml.safe_load(f.read_text()) or []:
            case["suite"] = f.stem
            cases.append(case)
    return cases


def substitute(value: Any, context: dict[str, Any]) -> Any:
    """Fill `{bag}`, `{mission_id}`, `{findings[0].id}` from the running context."""
    if isinstance(value, str):
        def repl(m: re.Match[str]) -> str:
            from evals.scoring import resolve_path

            key = m.group(1)
            if key in context:
                return str(context[key])
            resolved = resolve_path(context.get("last_result", {}), key)
            return str(resolved) if resolved is not None else m.group(0)

        out = PLACEHOLDER.sub(repl, value)
        return out
    if isinstance(value, dict):
        return {k: substitute(v, context) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute(v, context) for v in value]
    return value


async def call(server: Any, name: str, args: dict[str, Any]) -> Any:
    result = await server.call_tool(name, args)
    content = getattr(result, "content", None) or []
    for block in content:
        text = getattr(block, "text", None)
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"text": text}
    structured = getattr(result, "structuredContent", None)
    return structured if structured is not None else {}


async def run_case(server: Any, case: dict[str, Any], bag_dir: Path,
                   corpus_ctx: dict[str, Any]) -> CaseScore:
    score = CaseScore(case_id=case["id"], min_tool_calls=len(case.get("tools", [])) or 1)
    context: dict[str, Any] = dict(corpus_ctx)
    try:
        fixture = case.get("fixture", "clean")
        if fixture == "corpus":
            context.setdefault("bag", str(corpus_ctx.get("bag", "")))
        else:
            context["bag"] = str(fixtures.build(fixture, bag_dir))
    except Exception as exc:
        score.error = f"fixture: {type(exc).__name__}: {exc}"
        return score

    last: Any = {}
    for step in case.get("tools", []):
        args = substitute(step.get("args", {}), {**context, "last_result": last})
        try:
            last = await call(server, step["name"], args)
        except Exception as exc:
            score.error = f"{step['name']}: {type(exc).__name__}: {exc}"
            return score
        score.tool_calls += 1
        score.tokens += estimate_tokens(last)
        claims, cited = count_claims(last)
        score.claims += claims
        score.cited_claims += cited

    checks = [check(last, spec) for spec in case.get("assert", [])]
    score.assertions = checks
    score.correctness = (sum(1 for c in checks if c.passed) / len(checks)) if checks else 1.0
    return score


async def run_model_case_scored(
    client: Any,
    server: Any,
    case: dict[str, Any],
    bag_dir: Path,
    corpus_ctx: dict[str, Any],
    model: str,
) -> CaseScore:
    """Same case, but the model chooses the tool calls.

    Assertions are checked against *every* result the model collected rather than the
    last one: a model that reaches the answer by another route has still retrieved the
    right evidence, and scoring it against a reference ordering would measure obedience
    instead of competence.
    """
    from evals.model_loop import run_model_case

    score = CaseScore(case_id=case["id"], min_tool_calls=len(case.get("tools", [])) or 1)
    context: dict[str, Any] = dict(corpus_ctx)
    try:
        fixture = case.get("fixture", "clean")
        if fixture == "corpus":
            context.setdefault("bag", str(corpus_ctx.get("bag", "")))
        else:
            context["bag"] = str(fixtures.build(fixture, bag_dir))
    except Exception as exc:  # noqa: BLE001
        score.error = f"fixture: {type(exc).__name__}: {exc}"
        return score

    # The question alone never says *which* recording — the reference `tools` block
    # carries that in its args. Give the model the same handles, or it is being graded
    # on guessing a path rather than on using the tool surface.
    question = substitute(case.get("question") or case.get("prompt") or case["id"], context)
    handles = []
    if context.get("bag"):
        handles.append(f"recording path: {context['bag']}")
    if context.get("mission_id"):
        handles.append(f"mission_id: {context['mission_id']}")
    if context.get("mission_id_b"):
        handles.append(f"a second mission_id to compare against: {context['mission_id_b']}")
    prompt = question + ("\n\n" + "\n".join(handles) if handles else "")

    traj = await run_model_case(client, server, prompt, model=model)

    score.tool_calls = len(traj.tool_calls)
    score.tokens = traj.total_tokens
    score.error = traj.error
    score.answer = traj.final_text
    score.wall_seconds = traj.wall_seconds
    score.unsupported_numbers = traj.unsupported_numbers()

    for result in traj.tool_results:
        claims, cited = count_claims(result)
        score.claims += claims
        score.cited_claims += cited

    specs = case.get("assert", [])
    if not specs:
        score.correctness = 1.0 if not traj.error else 0.0
        return score

    checks: list[Any] = []
    for spec in specs:
        best = check({}, spec)
        for result in traj.tool_results:
            candidate = check(result, spec)
            if candidate.passed:
                best = candidate
                break
        checks.append(best)
    score.assertions = checks
    score.correctness = sum(1 for c in checks if c.passed) / len(checks)
    return score


async def run_all(
    only: str | None,
    bag_dir: Path,
    cache_dir: Path,
    model_client: Any | None = None,
    model: str = "",
) -> SuiteScore:
    import os

    os.environ["BAGLENS_CACHE_DIR"] = str(cache_dir)
    from baglens.config import load_config, set_config

    set_config(load_config())
    from baglens.server import build_server

    server = build_server()
    cases = load_cases(only)

    corpus_ctx: dict[str, Any] = {}
    if any(c.get("fixture") == "corpus" for c in cases):
        corpus_dir = bag_dir / "corpus"
        paths = fixtures.build_corpus(corpus_dir)
        await call(server, "catalog.add_source", {"path": str(corpus_dir), "background": False})
        listing = await call(server, "catalog.list_missions", {"limit": 10})
        missions = listing.get("missions", [])
        if missions:
            corpus_ctx["mission_id"] = missions[0]["mission_id"]
            corpus_ctx["mission_id_b"] = missions[min(1, len(missions) - 1)]["mission_id"]
            corpus_ctx["bag"] = missions[0]["path"]
        else:
            corpus_ctx["bag"] = str(paths[0])

    suite = SuiteScore()
    for case in cases:
        if model_client is not None:
            suite.cases.append(
                await run_model_case_scored(
                    model_client, server, case, bag_dir, corpus_ctx, model
                )
            )
            print(f"  {case['id']}: {suite.cases[-1].correctness * 100:.0f}% "
                  f"({suite.cases[-1].tool_calls} calls, "
                  f"{suite.cases[-1].tokens:,} tokens)")
        else:
            suite.cases.append(await run_case(server, case, bag_dir, corpus_ctx))
    return suite


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="evals/RESULTS.md")
    ap.add_argument("--only", default=None, help="run one suite (integrity|analysis|fleet)")
    ap.add_argument("--bags", default="/tmp/baglens-evals")
    ap.add_argument("--cache", default="/tmp/baglens-evalcache")
    ap.add_argument("--gate", action="store_true", help="exit non-zero if any case fails")
    ap.add_argument(
        "--model",
        nargs="?",
        const="claude-opus-5",
        default=None,
        help="run the model in the loop instead of the reference tool sequence "
             "(needs ANTHROPIC_API_KEY; costs money; never runs in CI)",
    )
    args = ap.parse_args(argv)

    model_client = None
    if args.model:
        from evals.model_loop import build_client

        try:
            model_client = build_client()
        except ImportError:
            print("model mode needs the anthropic SDK:  uv sync --extra evals")
            return 2
        except Exception as exc:  # noqa: BLE001
            print(f"could not build an Anthropic client: {type(exc).__name__}: {exc}")
            print("set ANTHROPIC_API_KEY, or run without --model for the deterministic path.")
            return 2
        print(f"model-in-the-loop mode: {args.model}\n")

    t0 = time.perf_counter()
    suite = asyncio.run(
        run_all(args.only, Path(args.bags), Path(args.cache), model_client, args.model or "")
    )
    wall = time.perf_counter() - t0

    if args.model:
        text = suite.render_model("baglens model-in-the-loop eval", args.model)
        text += f"\nRun in {wall:.1f}s against the live MCP tool surface.\n"
    else:
        text = suite.render("baglens tool-surface eval")
        text += (
            f"\nRun in {wall:.1f}s, deterministic mode (reference tool sequences, no model in "
            "the loop). Model-in-the-loop scoring is the same harness with `--model`, which is "
            "opt-in because it costs money and cannot run in CI.\n"
        )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text)
    print(text)

    failed = [c.case_id for c in suite.cases if not c.passed]
    if failed:
        print(f"{len(failed)} failing: {', '.join(failed)}")
    return 1 if (failed and args.gate) else 0


if __name__ == "__main__":
    raise SystemExit(main())
