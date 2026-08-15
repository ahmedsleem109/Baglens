"""D7 — cross-topic gap correlation.

The distinction that saves engineers days: **did the sensor die, or did the recorder
stall?** A 60-second sliding window of silent intervals across all topics; for each
gap, the fraction of other active topics also silent for at least half of it.

    concurrency > 0.7 → system-wide stall (recorder, disk, CPU, or power)
    concurrency < 0.2 → isolated topic failure (that sensor or node)
    otherwise         → subsystem failure — and the co-silent list *is* the diagnosis

If ``/camera/*`` all die together it is the camera driver or the USB bus, not three
sensors failing at once.

State: one pruned interval list per topic, bounded to the 60 s window.
"""

from __future__ import annotations

from collections import deque
from typing import Any

from ..config import CONFIG, Config
from ..models import Finding, GapDetail, Severity
from .gaps import Gap, severity_for


class SilentInterval:
    __slots__ = ("topic", "start", "end", "concurrency", "co_silent", "classification")

    def __init__(self, topic: str, start: float, end: float) -> None:
        self.topic = topic
        self.start = start
        self.end = end
        self.concurrency = 0.0
        self.co_silent: list[str] = []
        self.classification = "unknown"


class CorrelationDetector:
    name = "correlation"

    def __init__(self, cfg: Config | None = None, expected_topics: int = 0) -> None:
        self.cfg = cfg or CONFIG
        self.k = self.cfg.gap.k_by_sensitivity[self.cfg.sensitivity]
        #: how many topics the recording declares it will carry, 0 when unknown.
        #: The denominator floor below; see `_score`.
        self.expected_topics = expected_topics
        self.window: dict[str, deque[SilentInterval]] = {}
        self.first_seen: dict[str, float] = {}
        self.last_seen: dict[str, float] = {}
        self.results: list[SilentInterval] = []
        self._pending: dict[str, SilentInterval] = {}

    # -- streaming ---------------------------------------------------------

    def _prune(self, now: float) -> None:
        cutoff = now - self.cfg.correlation.window_s
        for dq in self.window.values():
            while dq and dq[0].end < cutoff:
                dq.popleft()

    def on_arrival(self, topic: str, t: float, dt: float | None, period: float | None) -> None:
        self.first_seen.setdefault(topic, t)
        self.last_seen[topic] = t
        if dt is None or not period:
            return
        threshold = max(self.k * period, self.cfg.gap.floor_s)
        if dt <= threshold:
            return

        interval = SilentInterval(topic, t - dt, t)
        dq = self.window.setdefault(topic, deque())
        merge_window = self.cfg.gap.merge_periods * period
        if dq and interval.start - dq[-1].end <= merge_window:
            dq[-1].end = interval.end
            interval = dq[-1]
        else:
            dq.append(interval)
            self.results.append(interval)
        self._prune(t)
        self._score(interval, t)

    def _score(self, interval: SilentInterval, now: float) -> None:
        """Concurrency against the *other topics that were alive before this gap*."""
        need = self.cfg.correlation.overlap_frac * (interval.end - interval.start)
        active_before = [
            tp
            for tp, first in self.first_seen.items()
            if tp != interval.topic and first <= interval.start
        ]
        if not active_before:
            return
        co: list[str] = []
        for tp in active_before:
            overlap = 0.0
            for other in self.window.get(tp, ()):  # only the 60 s window is retained
                lo = max(interval.start, other.start)
                hi = min(interval.end, other.end)
                if hi > lo:
                    overlap += hi - lo
            # a topic whose last message predates the gap start is silent for all of it
            if overlap < need and self.last_seen.get(tp, 0.0) <= interval.start:
                overlap = max(overlap, interval.end - max(interval.start, self.last_seen.get(tp, 0.0)))
            if overlap >= need:
                co.append(tp)
        interval.co_silent = sorted(co)
        # Floor the denominator on the topics the recording *declares*, not just the ones
        # seen so far. A PX4 flight logs a reduced topic set before arming and starts the
        # other ~105 at arm time; scored against only what had appeared, 11 co-silent
        # topics out of the 14 known so far read as 0.79 — a "system-wide stall" covering
        # 60% of the recording, on a flight whose own logger recorded 3.18s of dropout.
        # `max` means this can only ever lower concurrency, and only while topics are
        # still arriving: once everything has been seen the two denominators agree.
        # 0 when the source cannot enumerate its topics, which restores the old behaviour.
        denominator = max(len(active_before), self.expected_topics - 1)
        interval.concurrency = len(co) / denominator if denominator else 0.0
        c = self.cfg.correlation
        interval.classification = (
            "system_wide_stall"
            if interval.concurrency > c.system_wide
            else "isolated_topic"
            if interval.concurrency < c.isolated
            else "subsystem_failure"
        )

    # -- results -----------------------------------------------------------

    def classify(self, gaps: list[Gap]) -> list[GapDetail]:
        """Join the correlation verdicts onto the gap list D2 produced."""
        details: list[GapDetail] = []
        for g in gaps:
            match = min(
                (r for r in self.results if r.topic == g.topic),
                key=lambda r: abs(r.start - g.t_start),
                default=None,
            )
            hz = 1.0 / g.expected_period if g.expected_period else 0.0
            details.append(
                GapDetail(
                    topic=g.topic,
                    t_start=g.t_start,
                    t_end=g.t_end,
                    duration_s=round(g.duration, 4),
                    expected_period_s=round(g.expected_period, 6),
                    periods_missed=round(g.periods, 1),
                    severity=severity_for(g, self.cfg),
                    estimated_lost=max(0, int(round(g.duration * hz)) - 1),
                    concurrency=round(match.concurrency, 3) if match else 0.0,
                    co_silent_topics=match.co_silent if match else [],
                    classification=match.classification if match else "unknown",  # type: ignore[arg-type]
                )
            )
        return details

    def merged_stalls(self) -> list[SilentInterval]:
        """The system-wide stalls, merged into one interval per event.

        Exposed rather than kept inside `finalize` because the auditor needs these
        windows *before* it scores topics: a topic silenced by a shared stall should not
        be billed for it, and the per-topic gaps inside one are evidence for that single
        event rather than findings of their own.
        """
        c = self.cfg.correlation
        stalls = [r for r in self.results if r.concurrency > c.system_wide]
        merged: list[SilentInterval] = []
        for r in sorted(stalls, key=lambda r: r.start):
            if merged and r.start <= merged[-1].end + 0.5:
                merged[-1].end = max(merged[-1].end, r.end)
                merged[-1].co_silent = sorted(
                    set(merged[-1].co_silent) | {r.topic} | set(r.co_silent)
                )
            else:
                copy = SilentInterval(r.topic, r.start, r.end)
                copy.concurrency = r.concurrency
                copy.co_silent = sorted(set(r.co_silent) | {r.topic})
                merged.append(copy)
        return merged

    def finalize(self, t_end: float) -> list[Finding]:
        out: list[Finding] = []
        c = self.cfg.correlation
        # report the stalls, not every isolated gap — D2 already owns those
        merged = self.merged_stalls()

        for r in merged[:50]:
            out.append(
                Finding(
                    detector="correlation",
                    severity=Severity.CRITICAL if r.end - r.start > 5 else Severity.HIGH,
                    topic=None,
                    t_start=r.start,
                    t_end=r.end,
                    summary=(
                        f"system-wide stall: {len(r.co_silent)} topics silent together for "
                        f"{r.end - r.start:.2f}s"
                    ),
                    evidence={
                        "concurrency": round(r.concurrency, 3),
                        "duration_s": round(r.end - r.start, 3),
                        "topics_silent": float(len(r.co_silent)),
                    },
                    confidence=r.concurrency,
                    interpretation=(
                        "everything went quiet at once, so this is the recorder, the disk, the "
                        "CPU, or power — not a sensor. Looking at any single topic here will "
                        "send you down the wrong path: "
                        + ", ".join(r.co_silent[:8])
                    ),
                    rule=f"concurrency > {c.system_wide} over a {c.window_s}s window",
                )
            )

        subsystem = [
            r
            for r in self.results
            if c.isolated <= r.concurrency <= c.system_wide and len(r.co_silent) >= 2
        ]
        for r in subsystem[:20]:
            out.append(
                Finding(
                    detector="correlation",
                    severity=Severity.HIGH,
                    topic=r.topic,
                    t_start=r.start,
                    t_end=r.end,
                    summary=(
                        f"subsystem failure: {r.topic} and {len(r.co_silent)} related topics "
                        f"silent together for {r.end - r.start:.2f}s"
                    ),
                    evidence={"concurrency": round(r.concurrency, 3)},
                    confidence=0.7,
                    interpretation=(
                        "a shared driver, bus, or process died — co-silent topics: "
                        + ", ".join(r.co_silent[:8])
                    ),
                    rule=f"{c.isolated} <= concurrency <= {c.system_wide}",
                )
            )
        return out

    def state_bytes(self) -> int:
        return sum(64 * len(dq) for dq in self.window.values()) + 128

    # -- checkpoint --------------------------------------------------------

    def to_state(self) -> dict[str, Any]:
        # Every interval in `window` is the *same object* as one in `results` — a merge
        # extends `dq[-1]` in place and the result must be visible through both. Storing
        # the two independently would restore twins that drift apart on the next merge,
        # so `results` is canonical and `window` keeps indices into it.
        index = {id(iv): i for i, iv in enumerate(self.results)}
        return {
            "results": [_interval_state(iv) for iv in self.results],
            "window": {tp: [index[id(iv)] for iv in dq] for tp, dq in self.window.items()},
            "first_seen": dict(self.first_seen),
            "last_seen": dict(self.last_seen),
            # travels with the checkpoint: a resumed pass sees only the topics in its own
            # half, so re-deriving this would score the two halves on different scales
            "expected_topics": self.expected_topics,
        }

    @classmethod
    def from_state(cls, state: dict[str, Any], cfg: Config | None = None) -> CorrelationDetector:
        obj = cls(cfg, expected_topics=int(state.get("expected_topics", 0)))
        obj.results = [_interval_from(s) for s in state["results"]]
        obj.window = {
            tp: deque(obj.results[i] for i in idxs) for tp, idxs in state["window"].items()
        }
        obj.first_seen = {k: float(v) for k, v in state["first_seen"].items()}
        obj.last_seen = {k: float(v) for k, v in state["last_seen"].items()}
        return obj


def _interval_state(iv: SilentInterval) -> dict[str, Any]:
    return {"topic": iv.topic, "start": iv.start, "end": iv.end,
            "concurrency": iv.concurrency, "co_silent": list(iv.co_silent),
            "classification": iv.classification}


def _interval_from(s: dict[str, Any]) -> SilentInterval:
    iv = SilentInterval(str(s["topic"]), float(s["start"]), float(s["end"]))
    iv.concurrency = float(s["concurrency"])
    iv.co_silent = [str(x) for x in s["co_silent"]]
    iv.classification = str(s["classification"])
    return iv
