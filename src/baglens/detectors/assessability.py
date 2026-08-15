"""Whether this recording can be judged at all — the refusal path.

Every other module here answers "what is wrong with this recording?". This one answers
the question that comes first and that nothing was asking: **is there enough here to
answer that honestly?**

The failure mode it exists for is specific and measured. `nuway_stops` is a shuttle bus
that spent its recording parked; 70 of its 110 topics are event-driven, and one of their
silences became a 1,489-second "system-wide stall" on a 1,492-second file, published as
`compromised` at score 0.0. Every attempt to fix that inside the correlation detector cost
22+ points of recall against PX4's real dropout labels, because when a recorder genuinely
stops, event-driven topics stop too — their silence is evidence exactly like anyone
else's. See W15 in `PHASE3.md`.

So the bound is drawn one level up. The detectors keep their evidence; the *report* stops
claiming a verdict when too little of the recording could be assessed. A tool that says "I
cannot tell" is worth more than one that is confidently wrong, and being reliably neither
is the only thing here a competitor cannot copy in an afternoon.

Four independent ways a recording can fail to support a verdict, each a ratio against a
configured floor:

* **too few assessable topics** — most of what was recorded has no rate model, so most of
  the file was never actually checked;
* **too few assessable messages** — the assessable topics exist but carry almost none of
  the traffic, so the checks that did run saw almost nothing;
* **too little coverage** — the recording is mostly a hole. `nuway_stops` opens with 113
  seconds in which nothing at all publishes;
* **too short** — no topic could complete cadence warmup, so no baseline was ever
  established and every threshold downstream is a guess.

Bounded and streaming like everything else: the inputs are the per-topic health rows the
auditor already assembles and the timeline accumulator's fixed-width occupancy rows.
"""

from __future__ import annotations

from typing import Any

from ..config import CONFIG, Config
from ..models import Assessability, TopicHealth


def coverage_fraction(timeline: Any, topics: set[str]) -> float:
    """Fraction of the recording's buckets in which at least one named topic published.

    Read off the timeline accumulator, which is already bounded at `max_cols` buckets per
    topic — no new state, and no second pass. Restricted to the assessable topics on
    purpose: an event-driven topic firing once in an otherwise dead minute does not make
    that minute observed.
    """
    rows = [row for tp, row in getattr(timeline, "rows", {}).items() if tp in topics]
    if not rows:
        return 0.0
    n_cols = max(1, min(len(rows[0]), int(timeline.t_end / timeline.bucket_s) + 1))
    occupied = sum(
        1 for i in range(n_cols) if any(row[i] for row in rows)
    )
    return occupied / n_cols


def assess(
    topics: list[TopicHealth],
    duration_s: float,
    timeline: Any = None,
    cfg: Config | None = None,
) -> Assessability:
    """Can this recording support a verdict? Returns the answer and why.

    `confidence` is the worst of the four ratios, clipped to 1.0 — so it reads as "how
    close was the weakest link to the bar", and a recording that clears every bar reads
    1.0 rather than an arbitrary high number. A recording is refused when any ratio is
    below 1.0.
    """
    cfg = cfg or CONFIG
    a = cfg.assessability

    total = len(topics)
    assessable = [t for t in topics if t.hz_source != "aperiodic"]
    names = {t.topic for t in assessable}

    msgs_total = sum(t.count for t in topics)
    msgs_assessable = sum(t.count for t in assessable)

    topic_frac = len(assessable) / total if total else 0.0
    msg_frac = msgs_assessable / msgs_total if msgs_total else 0.0
    cover = coverage_fraction(timeline, names) if timeline is not None else 1.0

    ratios: dict[str, float] = {
        "topics": topic_frac / a.min_topic_fraction if a.min_topic_fraction else 1.0,
        "messages": msg_frac / a.min_message_fraction if a.min_message_fraction else 1.0,
        "coverage": cover / a.min_coverage if a.min_coverage else 1.0,
        "duration": duration_s / a.min_duration_s if a.min_duration_s else 1.0,
    }

    reasons: list[str] = []
    if ratios["topics"] < 1.0:
        reasons.append(
            f"only {len(assessable)} of {total} topics have a measurable publication rate "
            f"({100 * topic_frac:.0f}%, floor {100 * a.min_topic_fraction:.0f}%) — the rest "
            f"are event-driven or too sparse, so most of this recording was never checked"
        )
    if ratios["messages"] < 1.0:
        reasons.append(
            f"the topics that could be checked carry {100 * msg_frac:.0f}% of the messages "
            f"(floor {100 * a.min_message_fraction:.0f}%) — the checks that ran saw a "
            f"minority of what was recorded"
        )
    if ratios["coverage"] < 1.0:
        reasons.append(
            f"assessable topics published during {100 * cover:.0f}% of the recording "
            f"(floor {100 * a.min_coverage:.0f}%) — the rest is silence that cannot be "
            f"distinguished from a robot that was simply idle"
        )
    if ratios["duration"] < 1.0:
        reasons.append(
            f"the recording is {duration_s:.1f}s long (floor {a.min_duration_s:.0f}s) — "
            f"shorter than the cadence warmup, so no baseline was established and every "
            f"threshold below would be a guess"
        )

    return Assessability(
        assessable=not reasons,
        confidence=round(min(min(ratios.values()), 1.0), 3),
        topics_total=total,
        topics_assessable=len(assessable),
        message_fraction=round(msg_frac, 4),
        coverage_fraction=round(cover, 4),
        duration_s=round(duration_s, 3),
        reasons=reasons,
    )


#: what an analyst must not conclude from a recording the tool refused to judge
REFUSAL_CAVEAT = (
    "This recording could not be assessed, so the verdict is `unassessable` rather than a "
    "score. The findings below are real observations, but their **absence is not evidence "
    "of health**: do not read a short finding list here as a clean recording. Fix the "
    "recording setup — record the topics you intend to check, for long enough to establish "
    "a rate — and audit it again."
)
