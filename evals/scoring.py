"""Scoring for the eval suite.

Four axes, because "did it get the right answer" is not enough to compare tool surfaces:

* **correctness** — did the assertions on the tool results hold?
* **efficiency** — how many tool calls did the answer take against the minimum?
* **tokens** — how much context did the answers consume?
* **hallucination** — what fraction of claims came back without provenance? A tool
  surface that lets a model assert things it cannot cite is a broken tool surface.
"""

from __future__ import annotations

import json
import operator
import re
from dataclasses import dataclass, field
from typing import Any

OPS = {
    "eq": operator.eq,
    "ne": operator.ne,
    "gt": operator.gt,
    "gte": operator.ge,
    "lt": operator.lt,
    "lte": operator.le,
}


def resolve_path(obj: Any, path: str) -> Any:
    """Dotted path with list indexing and a `[]` wildcard: `findings[].detector`."""
    cur: Any = obj
    for part in path.split("."):
        if not part:
            continue
        m = re.fullmatch(r"([^\[\]]*)\[(\d*)\]", part)
        if m:
            name, idx = m.group(1), m.group(2)
            if name:
                cur = cur.get(name) if isinstance(cur, dict) else getattr(cur, name, None)
            if cur is None:
                return None
            if idx == "":
                return list(cur)
            try:
                cur = cur[int(idx)]
            except (IndexError, TypeError, KeyError):
                return None
        else:
            cur = cur.get(part) if isinstance(cur, dict) else getattr(cur, part, None)
        if cur is None:
            return None
    return cur


@dataclass
class AssertionResult:
    path: str
    op: str
    expected: Any
    actual: Any
    passed: bool
    note: str = ""


def check(result: Any, spec: dict[str, Any]) -> AssertionResult:
    path = spec.get("path", "")
    op = spec.get("op", "eq")
    expected = spec.get("value")
    actual = resolve_path(result, path)

    passed = False
    note = ""
    if op in OPS:
        try:
            passed = bool(OPS[op](actual, expected))
        except TypeError:
            passed = False
            note = f"type mismatch: {type(actual).__name__} vs {type(expected).__name__}"
    elif op == "contains":
        if isinstance(actual, str):
            passed = str(expected).lower() in actual.lower()
        elif isinstance(actual, (list, tuple, set, dict)):
            passed = expected in actual
    elif op == "not_contains":
        passed = not (
            (isinstance(actual, str) and str(expected).lower() in actual.lower())
            or (isinstance(actual, (list, tuple, set, dict)) and expected in actual)
        )
    elif op == "any_contains":
        values = actual if isinstance(actual, (list, tuple)) else [actual]
        passed = any(str(expected).lower() in str(v).lower() for v in values)
    elif op == "len_gte":
        passed = actual is not None and len(actual) >= int(expected)
    elif op == "len_eq":
        passed = actual is not None and len(actual) == int(expected)
    elif op == "approx":
        tol = float(spec.get("tolerance", 0.1))
        try:
            passed = abs(float(actual) - float(expected)) <= tol
        except (TypeError, ValueError):
            passed = False
    elif op == "non_empty":
        passed = bool(actual)
    elif op == "empty":
        passed = not actual
    else:
        note = f"unknown op {op}"
    return AssertionResult(path, op, expected, actual, passed, note)


@dataclass
class CaseScore:
    case_id: str
    correctness: float = 0.0
    tool_calls: int = 0
    min_tool_calls: int = 1
    tokens: int = 0
    claims: int = 0
    cited_claims: int = 0
    error: str = ""
    assertions: list[AssertionResult] = field(default_factory=list)
    #: model mode only — figures in the model's prose that appear in no tool result
    unsupported_numbers: list[str] = field(default_factory=list)
    answer: str = ""
    wall_seconds: float = 0.0

    @property
    def passed(self) -> bool:
        return not self.error and self.correctness >= 1.0

    @property
    def efficiency(self) -> float:
        return min(1.0, self.min_tool_calls / self.tool_calls) if self.tool_calls else 0.0

    @property
    def hallucination_rate(self) -> float:
        return 1.0 - (self.cited_claims / self.claims) if self.claims else 0.0


def count_claims(result: Any) -> tuple[int, int]:
    """Count assertive statements in a result and how many carry provenance.

    A "claim" is any finding, verdict, caveat or summary string the model would be
    entitled to repeat to a user. It is cited if a Provenance sits above it.
    """
    claims = 0
    cited = 0

    def has_prov(node: Any) -> bool:
        if not isinstance(node, dict):
            return False
        prov = node.get("provenance")
        return isinstance(prov, dict) and bool(prov.get("method") or prov.get("path"))

    def walk(node: Any, inherited: bool) -> None:
        nonlocal claims, cited
        if isinstance(node, dict):
            here = inherited or has_prov(node)
            for key, value in node.items():
                if key in ("summary", "verdict", "interpretation", "note") and isinstance(value, str) and value:
                    claims += 1
                    cited += 1 if here else 0
                elif key == "caveats" and isinstance(value, list):
                    claims += len(value)
                    cited += len(value) if here else 0
                else:
                    walk(value, here)
        elif isinstance(node, list):
            for item in node:
                walk(item, inherited)

    walk(result, False)
    return claims, cited


def estimate_tokens(payload: Any) -> int:
    return int(len(json.dumps(payload, default=str)) / 3.6) + 1


@dataclass
class SuiteScore:
    cases: list[CaseScore] = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.cases)

    @property
    def pass_rate(self) -> float:
        return sum(1 for c in self.cases if c.passed) / self.n if self.n else 0.0

    @property
    def mean_correctness(self) -> float:
        return sum(c.correctness for c in self.cases) / self.n if self.n else 0.0

    @property
    def mean_efficiency(self) -> float:
        return sum(c.efficiency for c in self.cases) / self.n if self.n else 0.0

    @property
    def total_tokens(self) -> int:
        return sum(c.tokens for c in self.cases)

    @property
    def mean_tokens(self) -> float:
        return self.total_tokens / self.n if self.n else 0.0

    @property
    def hallucination_rate(self) -> float:
        claims = sum(c.claims for c in self.cases)
        cited = sum(c.cited_claims for c in self.cases)
        return 1.0 - (cited / claims) if claims else 0.0

    @property
    def unsupported_number_rate(self) -> float:
        """Fraction of cases whose answer quoted a figure no tool returned."""
        if not self.cases:
            return 0.0
        return sum(1 for c in self.cases if c.unsupported_numbers) / self.n

    def render_model(self, title: str, model: str) -> str:
        """Model-in-the-loop report. Separate from `render` because the interesting
        columns differ: what the model *did* matters more than which assertion failed."""
        lines = [
            f"# {title}",
            "",
            f"Model: **`{model}`**. {self.n} cases, real tool surface, live agentic loop.",
            "",
            f"- pass rate: **{self.pass_rate * 100:.1f}%**",
            f"- mean correctness: {self.mean_correctness * 100:.1f}%",
            f"- mean tool-call efficiency: {self.mean_efficiency * 100:.1f}% "
            f"(reference sequence length / calls actually made)",
            f"- mean tokens per case: {self.mean_tokens:,.0f} (total {self.total_tokens:,})",
            f"- uncited claims in tool output: {self.hallucination_rate * 100:.2f}%",
            f"- **answers quoting an unsupported figure: {self.unsupported_number_rate * 100:.1f}%**",
            "",
            "| case | correct | calls | tokens | unsupported figures | detail |",
            "|---|---|---|---|---|---|",
        ]
        for c in sorted(self.cases, key=lambda c: (c.correctness, c.case_id)):
            failed = next((a for a in c.assertions if not a.passed), None)
            detail = c.error or (f"`{failed.path}` {failed.op} {failed.expected!r}" if failed else "")
            bad = ", ".join(c.unsupported_numbers[:3]) or "—"
            lines.append(
                f"| `{c.case_id}` | {c.correctness * 100:.0f}% | {c.tool_calls} | "
                f"{c.tokens:,} | {bad} | {detail[:80]} |"
            )
        lines += [
            "",
            "## How to read this",
            "",
            "**Correctness** here is not the deterministic runner's check. A model may reach "
            "the answer by a different route, so each case's assertions are evaluated against "
            "*every* tool result the model received — the question is whether it retrieved the "
            "right evidence, not whether it retrieved it in the reference order.",
            "",
            "**Efficiency** is the reference sequence length over the calls actually made, so "
            "1.0 means the model matched a hand-written path and lower means it explored. "
            "Exploring is not automatically worse.",
            "",
            "**Unsupported figures** are decimals and large integers in the model's prose that "
            "appear in no tool result it received, after allowing for rounding. It is a floor "
            "on how anchored the answer is, not a hallucination oracle.",
            "",
        ]
        return "\n".join(lines) + "\n"

    def render(self, title: str = "baglens eval results") -> str:
        lines = [
            f"# {title}",
            "",
            f"- cases: **{self.n}**",
            f"- pass rate: **{self.pass_rate * 100:.1f}%**",
            f"- mean correctness: {self.mean_correctness * 100:.1f}%",
            f"- mean tool-call efficiency: {self.mean_efficiency * 100:.1f}%",
            f"- mean tokens per case: {self.mean_tokens:,.0f} (total {self.total_tokens:,})",
            f"- uncited claims: **{self.hallucination_rate * 100:.2f}%**",
            "",
            "| case | correct | calls | tokens | uncited | failed assertion |",
            "|---|---|---|---|---|---|",
        ]
        for c in sorted(self.cases, key=lambda c: (c.correctness, c.case_id)):
            failed = next((a for a in c.assertions if not a.passed), None)
            detail = (
                c.error
                or (f"`{failed.path}` {failed.op} {failed.expected!r} (got {failed.actual!r})"
                    if failed else "")
            )
            lines.append(
                f"| `{c.case_id}` | {c.correctness * 100:.0f}% | {c.tool_calls} | "
                f"{c.tokens:,} | {c.hallucination_rate * 100:.0f}% | {detail[:110]} |"
            )
        return "\n".join(lines) + "\n"
