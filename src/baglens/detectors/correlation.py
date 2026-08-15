"""D7 — cross-topic gap correlation.

The distinction that saves engineers days: **did the sensor die, or did the recorder
stall?** A 60-second sliding window of silent intervals across all topics; for each
gap, the fraction of other active topics also silent for at least half of it.

    concurrency > 0.7 → system-wide stall (recorder, disk, CPU, or power)
    concurrency < 0.2 → isolated topic failure (that sensor or node)
    otherwise         → subsystem failure — and the co-silent list *is* the diagnosis

If ``/camera/*`` all die together it is the camera driver or the USB bus, not three
sensors failing at once.

State: one pruned interval list per topic, bounded to the 60 s window, plus at most
``max_results`` intervals of history behind it — ranked by concurrency, because the
longest silences in a recording are isolated slow topics and the stalls are seconds long.

This detector deliberately does **not** honour `unassessable`, unlike D2 and the per-topic
scores. Four versions of that rule were measured against PX4's own dropout labels and each
cost 22+ points of recall: when the recorder stops, event-driven topics stop too, so their
silence is evidence like anyone else's. See W15 in `PHASE3.md` before re-adding it.
"""

from __future__ import annotations

from bisect import bisect_left
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
        #: messages seen per topic — with first/last_seen this gives a lifetime mean
        #: spacing, which is the only nominal rate available for a topic D1 has
        #: declared aperiodic. Two ints per topic; see `_nominal`.
        self.count: dict[str, int] = {}
        self.period: dict[str, float] = {}
        self.results: list[SilentInterval] = []
        #: the same interval objects as `results`, grouped by topic and in start order —
        #: an index, not a second copy. Rebuilt from `results` on restore.
        self.by_topic: dict[str, list[SilentInterval]] = {}
        #: intervals dropped by the cap below, reported rather than hidden
        self.dropped_intervals = 0
        #: bumped whenever an interval is created, extended or rescored; `merged_stalls`
        #: caches against it
        self._revision = 0
        self._merged: list[SilentInterval] = []
        self._merged_rev = -1
        #: stalls rejected for covering essentially the whole stream — see `merged_stalls`
        self.implausible = 0
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
        self.count[topic] = self.count.get(topic, 0) + 1
        if period:
            self.period[topic] = period
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
            self.by_topic.setdefault(topic, []).append(interval)
        self._prune(t)
        self._cap_results()
        self._revision += 1
        self._score(interval, t)

    def _cap_results(self) -> None:
        """Keep `results` bounded — it is per-recording state, not per-window state.

        Every other accumulator in the library has a cap; this one did not, so a long
        mission with many short gaps grew it without limit. That breaks the constraint
        the whole library is built on, and it breaks it silently, because the list is
        only ever read at the end.

        Kept **by concurrency first, duration second** — not by duration alone, which is
        what D2 does with gaps and which is wrong here. The longest silences in a
        recording belong to slow isolated topics and score near zero concurrency; the
        stalls this detector exists to report are often a couple of seconds long. Ranking
        by duration therefore evicted exactly the intervals that carry findings, and cost
        35 of 152 labelled PX4 dropouts before anyone re-ran the corpus.

        Only intervals that have already left the 60 s window are eligible — one still in
        a window deque is live state that `to_state` indexes into.
        """
        cap = self.cfg.correlation.max_results
        if len(self.results) <= cap:
            return
        live = {id(iv) for dq in self.window.values() for iv in dq}
        keep = [iv for iv in self.results if id(iv) in live]
        old = sorted(
            (iv for iv in self.results if id(iv) not in live),
            key=lambda iv: (iv.concurrency, iv.end - iv.start),
            reverse=True,
        )
        room = max(0, cap - len(keep))
        self.dropped_intervals += max(0, len(old) - room)
        self.results = sorted(keep + old[:room], key=lambda iv: iv.start)
        self._reindex()

    def _reindex(self) -> None:
        self.by_topic = {}
        for iv in self.results:
            self.by_topic.setdefault(iv.topic, []).append(iv)

    def _nominal(self, topic: str) -> float:
        """The spacing this topic is expected to keep, or 0.0 when not yet knowable.

        D1's learned period when there is one. Event-driven topics have none — their
        modal inter-arrival describes the spacing inside a burst, not the burst spacing
        (see `CadenceConfig.aperiodic_ratio`) — so fall back to the lifetime mean, which
        is the honest rate for a bursty publisher and needs only the counter.
        """
        p = self.period.get(topic)
        if p:
            return p
        n = self.count.get(topic, 0)
        if n < 2:
            return 0.0
        span = self.last_seen[topic] - self.first_seen[topic]
        return span / (n - 1) if span > 0 else 0.0

    def _score(self, interval: SilentInterval, now: float) -> None:
        """Concurrency against the *other topics that were alive before this gap*."""
        length = interval.end - interval.start
        need = self.cfg.correlation.overlap_frac * length
        active_before = [
            tp
            for tp, first in self.first_seen.items()
            if tp != interval.topic and first <= interval.start
        ]
        if not active_before:
            return
        # Only topics that were *due to publish* during this interval carry any evidence.
        # A 1 Hz topic is idle across any 0.4 s window by construction, so its silence
        # says nothing either way — counting it as co-silent made short gaps on
        # event-driven topics collect a dozen bystanders each, and 236 of the corpus's
        # 242 unmatched findings were produced that way. Excluding them from the
        # numerator alone is equally wrong: it strips a genuine sub-second recorder stall
        # of the same bystanders and recall falls to 0.263. They must leave the
        # denominator too, so a short interval is judged only by the topics fast enough
        # to have been able to notice it.
        eligible = [tp for tp in active_before if 0.0 < self._nominal(tp) < length]
        if not eligible:
            return
        co: list[str] = []
        for tp in eligible:
            overlap = 0.0
            for other in self.window.get(tp, ()):  # only the 60 s window is retained
                lo = max(interval.start, other.start)
                hi = min(interval.end, other.end)
                if hi > lo:
                    overlap += hi - lo
            # a topic whose last message predates the gap start is silent for all of it,
            # and eligibility above already establishes that it was due within it
            silent_since = self.last_seen.get(tp, 0.0)
            if overlap < need and silent_since <= interval.start:
                overlap = max(overlap, interval.end - max(interval.start, silent_since))
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
        # Scaled by the eligible share of the topics actually seen: the floor stands in
        # for topics that have not started yet, and there is no reason to assume they are
        # any faster or slower than the ones that have.
        floor = (self.expected_topics - 1) * len(eligible) / len(active_before)
        denominator = max(len(eligible), floor)
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

    def _nearest(self, topic: str, t: float) -> SilentInterval | None:
        """The interval on `topic` whose start is closest to `t`.

        This used to be a `min` over every interval the detector had ever recorded, once
        per gap — quadratic, and the single largest cost in a snapshot: 56 ms of a 162 ms
        round trip on a 311 s flight, which is why a monitor snapshotting at 1 Hz spent
        more time reporting than auditing. Intervals arrive in start order per topic and
        their starts never move, so the same answer comes from one bisect.
        """
        by_start = self.by_topic.get(topic)
        if not by_start:
            return None
        i = bisect_left(by_start, t, key=lambda iv: iv.start)
        best = None
        for cand in by_start[max(0, i - 1): i + 1]:
            if best is None or abs(cand.start - t) < abs(best.start - t):
                best = cand
        return best

    def classify(self, gaps: list[Gap]) -> list[GapDetail]:
        """Join the correlation verdicts onto the gap list D2 produced."""
        details: list[GapDetail] = []
        for g in gaps:
            match = self._nearest(g.topic, g.t_start)
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
        # Memoised on a revision counter: the auditor asks once to exclude stall windows
        # from per-topic scoring and `finalize` asks again to report them, and on a live
        # monitor that is two full sorts of the interval list per snapshot. The counter
        # rather than `len(results)` because an arrival can extend and rescore an
        # existing interval — pushing it over the system-wide threshold — without
        # creating one, and that answer must not be served from the cache.
        if self._merged_rev == self._revision:
            return self._merged
        c = self.cfg.correlation
        stalls = [r for r in self.results if r.concurrency > c.system_wide]
        # A "stall" covering essentially the whole stream is not a stall — it is this
        # detector failing to model the recording. On a real shuttle-bus rosbag where 70
        # of 110 topics are event-driven, one interval opened by `/rosout` going quiet on
        # a stationary bus grew to 1,489 of 1,492 seconds, and the file was published as
        # `compromised` at score 0.0. Rejecting it by *shape* rather than by which topics
        # took part is what keeps the labelled corpus intact: every rule that instead
        # excluded event-driven topics cost 22+ points of real recall, because when the
        # recorder truly stops those topics stop too. Reported, never silently dropped.
        span = max((r.end for r in self.results), default=0.0) - min(
            (r.start for r in self.results), default=0.0
        )
        horizon = max(span, max(self.last_seen.values(), default=0.0))
        limit = c.max_stall_fraction * horizon
        if limit > 0:
            kept = [r for r in stalls if r.end - r.start <= limit]
            self.implausible = len(stalls) - len(kept)
            stalls = kept
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
        self._merged, self._merged_rev = merged, self._revision
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
        if self.implausible:
            out.append(
                Finding(
                    detector="correlation",
                    severity=Severity.INFO,
                    topic=None,
                    t_start=0.0,
                    t_end=t_end,
                    summary=(
                        f"{self.implausible} candidate stall(s) spanned more than "
                        f"{100 * c.max_stall_fraction:.0f}% of the recording and were not "
                        f"reported as stalls"
                    ),
                    evidence={"rejected": float(self.implausible)},
                    interpretation=(
                        "a recording whose topics are mostly event-driven can make one "
                        "silence look like a stall covering everything. That is this "
                        "detector failing to model the recording, not a finding about it "
                        "— treat the recorder as unassessed here rather than as healthy"
                    ),
                    rule=f"stall duration <= {c.max_stall_fraction} x recording",
                )
            )
        if self.dropped_intervals:
            out.append(
                Finding(
                    detector="correlation",
                    severity=Severity.INFO,
                    topic=None,
                    t_start=0.0,
                    t_end=t_end,
                    summary=(
                        f"{self.dropped_intervals} silent intervals were discarded; only "
                        f"the {c.max_results} longest are retained"
                    ),
                    evidence={"intervals_omitted": float(self.dropped_intervals)},
                    interpretation=(
                        "interval storage is bounded by design; the omitted ones are the "
                        "shortest and had already left the correlation window"
                    ),
                    rule=f"max_results={c.max_results}",
                )
            )
        return out

    def state_bytes(self) -> int:
        # `results` outlives the window and is now capped, so it belongs in the estimate:
        # leaving it out was how it grew unbounded without the budget ever noticing
        return sum(64 * len(dq) for dq in self.window.values()) + 64 * len(self.results) + 128

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
            "count": dict(self.count),
            "period": dict(self.period),
            "dropped_intervals": self.dropped_intervals,
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
        obj.count = {k: int(v) for k, v in state.get("count", {}).items()}
        obj.period = {k: float(v) for k, v in state.get("period", {}).items()}
        obj.dropped_intervals = int(state.get("dropped_intervals", 0))
        obj._reindex()  # an index over `results`, so it is derived rather than stored
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
