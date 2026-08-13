"""G2 — the response budgeter.

An LLM asked to "compare 10 missions" will happily try to read raw messages and die
at message 400. Every tool result passes through here: estimate, and if over budget
apply the tool's reduction ladder, then say so and teach the agent how to ask better.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable, Sequence
from typing import Any

from pydantic import BaseModel

from .config import CONFIG, Config
from .models import Budgeted


def estimate_tokens(obj: Any, cfg: Config | None = None) -> int:
    """Network-free token estimate. Deliberately crude and slightly pessimistic."""
    cfg = cfg or CONFIG
    if isinstance(obj, BaseModel):
        text = obj.model_dump_json()
    elif isinstance(obj, str):
        text = obj
    else:
        text = json.dumps(obj, default=str)
    return int(len(text) / cfg.budget.chars_per_token) + 1


def make_continuation(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def read_continuation(token: str) -> dict[str, Any]:
    pad = "=" * (-len(token) % 4)
    return json.loads(base64.urlsafe_b64decode(token + pad))


def decimate(seq: Sequence[Any], limit: int) -> list[Any]:
    """Evenly spaced subsample preserving first and last."""
    n = len(seq)
    if n <= limit or limit <= 0:
        return list(seq)
    if limit == 1:
        return [seq[0]]
    step = (n - 1) / (limit - 1)
    return [seq[int(round(i * step))] for i in range(limit)]


def apply_budget[T: BaseModel](
    result: T,
    ladder: Sequence[Callable[[T], T]] = (),
    cfg: Config | None = None,
    max_tokens: int | None = None,
    narrowing: str | None = None,
    continuation: dict[str, Any] | None = None,
) -> T:
    """Run ``result`` down its reduction ladder until it fits.

    ``ladder`` steps go from least to most lossy: raw values → decimated values →
    binned stats → summary sentence. Each step returns a new model.
    """
    cfg = cfg or CONFIG
    limit = max_tokens or cfg.budget.max_tokens
    size = estimate_tokens(result, cfg)
    if size <= limit:
        return result

    original = size
    current = result
    for step in ladder:
        current = step(current)
        size = estimate_tokens(current, cfg)
        if size <= limit:
            break

    if isinstance(current, Budgeted):
        current.truncated = True
        current.original_size = original
        if continuation is not None:
            current.continuation_token = make_continuation(continuation)
        current.suggested_narrowing = narrowing or (
            f"response was ~{original} tokens against a {limit} budget — "
            "narrow the time range or the topic list, or raise bin_seconds"
        )
    return current
