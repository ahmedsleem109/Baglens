"""Health score and caveats.

The formula is published in the docs on purpose. An opaque score is ignored; a
legible one gets argued about, and arguments are engagement.

Weighting the *minimum* topic heavily is deliberate: one broken critical topic
compromises an investigation regardless of how healthy the other forty are.
"""

from __future__ import annotations

from ..config import CONFIG, Config
from ..models import FileIntegrity, Finding, Severity, TopicHealth


def topic_score(
    th: TopicHealth, findings: list[Finding], cfg: Config | None = None, duration_s: float = 0.0
) -> float:
    cfg = cfg or CONFIG
    w = cfg.score
    expected_total = (th.expected_hz or th.observed_hz or 0.0)

    # Silence is judged against the length of the recording, not against a fixed number
    # of seconds: five seconds missing from a two-minute run is a serious hole, and the
    # same five seconds in an eight-hour run is a hiccup.
    silence_budget = max(5.0, 0.05 * duration_s) if duration_s > 0 else 30.0
    # Only silence this topic is actually responsible for. Time lost to a system-wide
    # stall is charged once, at the report level, instead of to every topic that
    # happened to be running when the recorder stopped.
    own_silence = max(0.0, th.total_silent_s - th.stall_silent_s)
    gap_penalty = min(1.0, own_silence / silence_budget) if own_silence else 0.0
    drop_rate = 0.0
    if expected_total > 0 and th.count:
        drop_rate = min(1.0, th.estimated_dropped / max(th.count + th.estimated_dropped, 1))
    jitter_excess = 0.0
    if th.jitter_cv > 0.2:
        jitter_excess = min(1.0, (th.jitter_cv - 0.2) / 0.8)
    degradation = 0.0
    for f in findings:
        if f.detector == "rate_degradation" and f.topic == th.topic:
            degradation = max(degradation, min(1.0, abs(f.evidence.get("relative_slope", 0.0))))

    score = 100.0 * (
        1.0
        - w.w_gap * gap_penalty
        - w.w_drop * drop_rate
        - w.w_jitter * jitter_excess
        - w.w_degradation * degradation
    )
    return round(max(0.0, min(100.0, score)), 1)


def file_score(fi: FileIntegrity | None) -> float:
    return fi.score if fi is not None else 100.0


def overall_score(
    topics: list[TopicHealth],
    fscore: float,
    cfg: Config | None = None,
    duration_s: float = 0.0,
) -> float:
    """Blend the worst topic, the average topic, file integrity — then charge shared
    stalls once.

    Per-topic scores deliberately ignore system-wide stalls (see `topic_score`), so the
    cost of the recorder stopping is applied here instead, in proportion to how much of
    the recording it swallowed. Losing 5% of a flight to logger stalls is normal on real
    hardware and should dent the score; losing 40% should dominate it.
    """
    cfg = cfg or CONFIG
    w = cfg.score
    if not topics:
        return fscore
    # Aperiodic topics carry no rate model, so their score is a placeholder rather than
    # a judgement. Including them would let an unassessable topic set `min()` — which is
    # weighted at 0.5 — and decide the verdict for the whole recording.
    assessed = [t for t in topics if t.hz_source != "aperiodic"]
    scores = [t.score for t in assessed] or [t.score for t in topics]
    base = w.a_min * min(scores) + w.b_mean * (sum(scores) / len(scores)) + w.c_file * fscore

    if duration_s > 0:
        # The stall cost the whole recording, so take the worst-affected topic rather
        # than an average that a hundred quiet topics would dilute away.
        stalled = max((t.stall_silent_s for t in topics), default=0.0)
        fraction = min(1.0, stalled / duration_s)
        base *= 1.0 - w.w_stall * fraction
    return max(0.0, base)


def verdict_for(overall: float, fscore: float, cfg: Config | None = None) -> str:
    """Map scores to a verdict, with file integrity acting as a ceiling rather than a term.

    A corrupt file whose surviving half looks healthy would otherwise score well: the
    detectors only ever saw the part that decodes, and that part is internally
    consistent. Integrity is a precondition for trusting anything else, so it caps the
    verdict instead of being averaged into it.
    """
    cfg = cfg or CONFIG
    s = cfg.score
    verdict = (
        "trustworthy" if overall >= s.trustworthy
        else "usable_with_caveats" if overall >= s.usable
        else "compromised"
    )
    if fscore < s.usable:
        return "compromised"
    if fscore < s.trustworthy and verdict == "trustworthy":
        return "usable_with_caveats"
    return verdict


def build_caveats(
    findings: list[Finding], topics: list[TopicHealth], fi: FileIntegrity | None
) -> list[str]:
    """What an analyst must NOT conclude from this data.

    This field matters more than it looks: it is the auditor telling the agent what
    conclusions the recording cannot support, which prevents a large class of
    confident-but-wrong answers.
    """
    out: list[str] = []
    by_topic: dict[str, list[Finding]] = {}
    for f in findings:
        if f.topic:
            by_topic.setdefault(f.topic, []).append(f)

    for th in topics:
        fs = by_topic.get(th.topic, [])
        worst_gap = max((f for f in fs if f.detector == "gap"), key=lambda f: f.severity, default=None)
        if worst_gap is not None and worst_gap.severity >= Severity.MEDIUM:
            out.append(
                f"{th.topic} was silent between t={worst_gap.t_start:.1f}–{worst_gap.t_end:.1f}s; "
                f"do not compute rates, counts, or coverage over that window."
            )
        if th.estimated_dropped and th.count:
            pct = 100.0 * th.estimated_dropped / (th.count + th.estimated_dropped)
            if pct >= 5.0:
                out.append(
                    f"{th.topic} lost ~{pct:.0f}% of its messages; any per-message statistic "
                    f"on this topic is biased and detection-rate style metrics are invalid."
                )
        if any(f.detector == "rate_degradation" for f in fs):
            out.append(
                f"{th.topic} changed rate substantially during the recording; comparing its "
                f"early and late behaviour without normalising by rate will mislead."
            )

    if any(f.detector == "clock" and f.severity >= Severity.CRITICAL for f in findings):
        out.append(
            "Timestamps are non-monotonic in this recording; time-range queries and any "
            "duration computed across the affected instants are unreliable."
        )
    if any(f.detector == "clock_lag" for f in findings):
        out.append(
            "The recorder was falling behind the publishers; message loss in the later part "
            "of this file is expected even where no gap is listed."
        )
    if any(f.detector == "correlation" for f in findings):
        out.append(
            "At least one silence was system-wide, so it reflects the recording host rather "
            "than any sensor; do not attribute those windows to a specific subsystem."
        )
    if fi is not None and (fi.truncated_bytes or not fi.has_summary):
        out.append(
            "The file is incomplete; absence of data after the last readable record is "
            "not evidence of absence of events."
        )
    if fi is not None and fi.unreadable_fraction > 0.01:
        out.append(
            f"{fi.unreadable_fraction * 100:.0f}% of the messages this file claims to "
            "contain do not decode — the recording is corrupt, and every count, rate and "
            "coverage figure below is computed only over what survived."
        )
    return out
