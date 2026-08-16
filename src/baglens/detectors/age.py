"""F1 — end-to-end data age.

The question that decides whether a robot is safe is not "is `/camera` publishing at
30 Hz?" but **"how old was the camera frame that produced this steering command?"** A
pipeline that drifts from 80 ms to 300 ms makes the robot quietly worse; the behaviour
looks like a tuning problem, and nobody can say which stage grew.

`header.stamp` is the capture time, and nodes propagate it as they pass derived results
along. Following it gives the true age of the information behind every command, per
stage. Nothing here is declared: the propagation graph is *inferred* from stamp equality,
because nobody tells you that `/detections` derives from `/camera`.

What this module refuses to do is as important as what it does:

* a topic whose schema carries no header is **unmeasurable**, and is reported as
  unmeasurable — never as an age computed from arrival time, which would be a number
  with no meaning presented next to numbers that have one;
* a node that restamps with "now" has destroyed the trace, so the ages behind it are not
  real ages. That is itself the finding;
* ages measured across two unsynchronised clocks are meaningless, so every finding here
  is gated on the clock detector having found no skew.

Bounded and streaming like everything else. Per topic: three P² estimators (five floats
each), one bucket ring, one small candidate-upstream counter. Shared: one fixed-capacity
table of in-flight stamps. Nothing grows with the length of the recording.
"""

from __future__ import annotations

from collections import OrderedDict, deque
from typing import Any

from ..config import CONFIG, Config
from ..models import Finding, Severity
from .base import P2Quantile, kendall_tau_p, theil_sen


class _TopicAge:
    """Per-topic age state. Fixed size."""

    __slots__ = ("topic", "p50", "p95", "p99", "n", "n_unstamped", "n_unset", "n_ahead",
                 "n_origin", "n_offclock", "bucket_p99", "baseline_p99",
                 "_baseline_samples", "n_buckets_kept", "_bucket", "_bucket_end", "_bucket_index",
                 "_t_origin", "upstream_counts", "stage_p50", "stage_p95", "stage_p99",
                 "first_t", "last_t")

    def __init__(self) -> None:
        # age of the data this topic publishes, measured from capture
        self.p50 = P2Quantile(0.50)
        self.p95 = P2Quantile(0.95)
        self.p99 = P2Quantile(0.99)
        # the delay this stage itself adds, measured from its inferred upstream
        self.stage_p50 = P2Quantile(0.50)
        self.stage_p95 = P2Quantile(0.95)
        self.stage_p99 = P2Quantile(0.99)
        self.n = 0
        #: schema has no stamp field at all
        self.n_unstamped = 0
        #: the field exists and was left at zero
        self.n_unset = 0
        #: stamped ahead of its own publish time
        self.n_ahead = 0
        #: carried a stamp no upstream topic had published — this topic is a stamp origin
        self.n_origin = 0
        #: age so large it can only be a different time base, not old data
        self.n_offclock = 0
        self.first_t: float | None = None
        self.last_t = 0.0
        #: per-bucket P99 age, for the in-window trend
        self.bucket_p99: deque[float] = deque()
        #: The mission's opening P99, frozen once and never revisited — two floats that
        #: outlive the ring. Without it a ramp that finishes and plateaus scrolls out of
        #: the 30-bucket window and reads as flat, which is how the first version scored
        #: 0.222 on real recordings while scoring 1.000 on synthetic ones.
        self.baseline_p99: float | None = None
        #: buckets that held enough samples for their P99 to mean anything
        self.n_buckets_kept = 0
        self._baseline_samples: list[float] = []
        self._bucket: P2Quantile | None = None
        self._bucket_end: float | None = None
        self._bucket_index = 0
        self._t_origin: float | None = None
        #: candidate upstream topic -> times its stamp arrived here first
        self.upstream_counts: dict[str, int] = {}

    def upstream(self, cfg: Any) -> tuple[str, int] | None:
        """The most-supported upstream, if the support is strong enough to believe."""
        if not self.upstream_counts:
            return None
        topic, count = max(self.upstream_counts.items(), key=lambda kv: kv[1])
        if count < cfg.min_link_observations:
            return None
        if self.n and count / self.n < cfg.min_link_fraction:
            return None
        return topic, count


class DataAgeDetector:
    """F1. One instance per audit; cross-topic by nature, like D7."""

    name = "data_age"

    def __init__(self, cfg: Config | None = None) -> None:
        self.cfg = cfg or CONFIG
        self.topics: dict[str, _TopicAge] = {}
        #: stamp_ns -> (origin_topic, origin_pub_t, last_topic, last_pub_t).
        #: An LRU because only stamps still in flight can be matched; capacity is the
        #: bound, and evictions are counted so the truncation is reported rather than
        #: quietly costing links (the D2 lesson).
        self._stamps: OrderedDict[int, tuple[str, int, str, int]] = OrderedDict()
        self._evictions = 0
        # Hoisted out of the per-message path. `cfg` is a proxy, so every `self.cfg.x.y`
        # in `on_arrival` is two __getattr__ calls per message — 1.4 M of them on a
        # 200 k-message recording, which showed up in the profile as a second of runtime.
        d = self.cfg.data_age
        self._horizon_ns = int(d.link_horizon_s * 1e9)
        self._table_size = d.stamp_table_size
        self._max_candidates = d.max_candidates_per_topic
        self._bucket_s = d.bucket_s
        self._n_buckets = d.n_buckets
        self._max_age_ns = int(d.max_plausible_age_s * 1e9)
        self._min_buckets = d.min_buckets
        self._min_samples = d.min_samples_per_bucket
        #: set by the auditor from the clock detector: ages across skewed clocks are not
        #: ages, and every finding is withheld when this is True
        self.clock_suspect = False
        #: topics whose schema has no stamp at all — the unmeasurable set
        self.unmeasurable: dict[str, str] = {}
        self._findings: list[Finding] = []

    # -- streaming ---------------------------------------------------------

    def on_arrival(self, topic: str, t: float, pub_t_ns: int, stamp_ns: int | None,
                   t0_ns: int, msg_type: str = "") -> None:
        """One arrival. ``t`` is bag-relative seconds; the rest is absolute nanoseconds.

        Ages are differenced in integer nanoseconds on purpose. In float seconds a stage
        that stamps with its own publish time lands at -1e-16 and gets thrown away as a
        stamp from the future — which silently deleted half the links in the restamping
        fixture before this was measured.
        """
        st = self.topics.get(topic)
        if st is None:
            st = self.topics[topic] = _TopicAge()
            st.topic = topic
        st.n += 1
        if st.first_t is None:
            st.first_t = t
        st.last_t = t

        if stamp_ns is None:
            st.n_unstamped += 1
            if topic not in self.unmeasurable:
                self.unmeasurable[topic] = (
                    f"{msg_type or 'this message type'} carries no header.stamp"
                )
            return

        if stamp_ns == 0:
            # the field exists and was never set. Not an age of fifty-five years.
            st.n_unset += 1
            return

        age_ns = pub_t_ns - stamp_ns
        if age_ns < 0:
            # a stamp genuinely ahead of its own publish: a real fault, but not an age
            st.n_ahead += 1
            return
        if age_ns > self._max_age_ns:
            # not old data — a different clock. Subtracting a steady-clock stamp from a
            # wall-clock publish gives an age of decades, and one such topic averaged into
            # a report makes every honest number in it look arbitrary.
            st.n_offclock += 1
            return

        age = age_ns / 1e9
        st.p50.push(age)
        st.p95.push(age)
        st.p99.push(age)
        self._push_bucket(st, t, age)

        # -- propagation graph, inferred from stamp equality
        prev = self._stamps.get(stamp_ns)
        if prev is None:
            # nothing upstream carried this stamp: this topic originated it
            st.n_origin += 1
            if len(self._stamps) >= self._table_size:
                _key, ev = self._stamps.popitem(last=False)
                # Most evictions are stamps nobody was ever going to match: on a bag with
                # no pipeline at all, every message inserts one. Only an eviction that is
                # still young enough to have found a downstream is a lost link, and only
                # those are worth reporting as truncation.
                if pub_t_ns - ev[3] < self._horizon_ns:
                    self._evictions += 1
            self._stamps[stamp_ns] = (topic, pub_t_ns, topic, pub_t_ns)
            return

        origin_topic, origin_pub, last_topic, last_pub = prev
        self._stamps.move_to_end(stamp_ns)
        if last_topic != topic:
            counts = st.upstream_counts
            if last_topic in counts:
                counts[last_topic] += 1
            elif len(counts) < self._max_candidates:
                counts[last_topic] = 1
            stage_delay_ns = pub_t_ns - last_pub
            if stage_delay_ns >= 0:
                delay = stage_delay_ns / 1e9
                st.stage_p50.push(delay)
                st.stage_p95.push(delay)
                st.stage_p99.push(delay)
        self._stamps[stamp_ns] = (origin_topic, origin_pub, topic, pub_t_ns)

    def _push_bucket(self, st: _TopicAge, t: float, age: float) -> None:
        bucket_s = self._bucket_s
        if st._bucket_end is None:
            st._bucket_end = t + bucket_s
            st._t_origin = t + bucket_s / 2
            st._bucket = P2Quantile(0.99)
            st._bucket.push(age)
            return
        while t >= st._bucket_end:
            if st._bucket is not None and st._bucket.count >= self._min_samples:
                closed = st._bucket.value
                st.bucket_p99.append(closed)
                if len(st.bucket_p99) > self._n_buckets:
                    st.bucket_p99.popleft()
                st.n_buckets_kept += 1
                if st.baseline_p99 is None:
                    st._baseline_samples.append(closed)
                    if len(st._baseline_samples) >= self._min_buckets:
                        # median, not mean: one slow bucket during node startup must not
                        # define what this topic's healthy age is for the rest of the run
                        s = sorted(st._baseline_samples)
                        st.baseline_p99 = s[len(s) // 2]
                        st._baseline_samples = []
                st._bucket_index += 1
            st._bucket = P2Quantile(0.99)
            st._bucket_end += bucket_s
        if st._bucket is not None:
            st._bucket.push(age)

    # -- close -------------------------------------------------------------

    def finalize(self, t_end: float) -> list[Finding]:
        d = self.cfg.data_age
        out: list[Finding] = list(self._findings)

        if self.clock_suspect:
            # One finding instead of a page of them. Ages measured across two clocks are
            # not ages, and publishing them with a caveat invites someone to read past it.
            return [Finding(
                detector=self.name,
                severity=Severity.INFO,
                t_start=0.0,
                t_end=t_end,
                summary="Data age not reported: publisher clocks disagree, so an age "
                        "measured across them would be meaningless",
                rule="data_age withheld when clock skew is detected",
                interpretation="Sync the clocks (chrony/PTP) and re-run; the ages are "
                               "computable but would be wrong until then.",
            )]

        # topics something downstream actually derives from. A node that stamps with its
        # own publish time only *destroyed* something if someone was reading the trace
        # through it; on a topic nothing derives from, the same stamp is just a design
        # choice, and reporting it would put a finding on most topics of most recordings.
        has_downstream = {
            up[0] for st in self.topics.values() if (up := st.upstream(d)) is not None
        }

        for topic, st in sorted(self.topics.items()):
            measured = st.n - st.n_unstamped
            if measured >= d.min_link_observations:
                out.extend(self._stamp_hygiene(
                    topic, st, measured, topic in has_downstream
                ))
            out.extend(self._trend(topic, st, t_end))

        if self._evictions:
            out.append(Finding(
                detector=self.name,
                severity=Severity.INFO,
                t_start=0.0,
                t_end=t_end,
                summary=f"Stamp table overflowed {self._evictions} times; some pipeline "
                        f"links may be missing",
                evidence={"evictions": float(self._evictions),
                          "capacity": float(d.stamp_table_size)},
                rule=f"bounded stamp table of {d.stamp_table_size} entries",
                interpretation="Bounded state, reported rather than hidden. Raise "
                               "data_age.stamp_table_size if links are missing.",
            ))
        return out

    def _stamp_hygiene(self, topic: str, st: _TopicAge, measured: int,
                       has_downstream: bool) -> list[Finding]:
        """Three ways a stamp is present but carries no capture information.

        The honest limit here is worth stating: from the data alone, a node that restamps
        with "now" and a genuine sensor are both *stamp origins*. What separates them is
        that a real sensor's capture always predates its own publish — the camera in the
        fixtures by 12 ms — whereas a restamping node's stamp **is** its publish time. So
        the discriminator is a near-zero age at an origin, not the origin itself. Which
        of the two a given topic is, is a question only an expectation can settle; F2's
        baseline is where that belongs.
        """
        d = self.cfg.data_age
        out: list[Finding] = []
        span = (st.first_t or 0.0, st.last_t)

        if st.n_unset / measured >= d.restamp_fraction:
            out.append(Finding(
                detector=self.name, severity=Severity.MEDIUM, topic=topic,
                t_start=span[0], t_end=span[1],
                summary=f"{topic} has a header.stamp but never sets it",
                evidence={"unset_fraction": round(st.n_unset / measured, 3),
                          "messages": float(measured)},
                rule=f"zero-stamp fraction >= {d.restamp_fraction}",
                interpretation="Nothing downstream can know how old this data is, and "
                               "any consumer doing a TF lookup at this stamp is looking "
                               "up the epoch.",
            ))
            return out

        if st.n_offclock / measured >= d.restamp_fraction:
            self.unmeasurable.setdefault(
                topic,
                "stamps are on a different time base from the recorder, so no age can be "
                "computed from them",
            )
            out.append(Finding(
                detector=self.name, severity=Severity.LOW, topic=topic,
                t_start=span[0], t_end=span[1],
                summary=f"{topic} stamps are on a different clock, so its data age is "
                        f"not measurable",
                evidence={"offclock_fraction": round(st.n_offclock / measured, 3),
                          "messages": float(measured)},
                rule=f"implied age > {d.max_plausible_age_s:.0f} s",
                interpretation="Usually a node stamping from a steady/monotonic clock "
                               "rather than the system clock. Harmless in itself, but "
                               "nothing downstream can align this topic in time.",
            ))
            return out

        if st.n_ahead / measured >= d.restamp_fraction:
            out.append(Finding(
                detector=self.name, severity=Severity.HIGH, topic=topic,
                t_start=span[0], t_end=span[1],
                summary=f"{topic} is stamped ahead of its own publish time",
                evidence={"ahead_fraction": round(st.n_ahead / measured, 3),
                          "messages": float(measured)},
                rule=f"future-stamp fraction >= {d.restamp_fraction}",
                interpretation="Either the publisher's clock leads the recorder's, or it "
                               "stamps a predicted time. Consumers will see "
                               "'extrapolation into the future' on every lookup.",
            ))
            return out

        origin_frac = st.n_origin / measured
        if (has_downstream and origin_frac >= d.restamp_fraction and st.p95.count
                and st.p95.value <= d.restamp_max_age_s):
            out.append(Finding(
                detector=self.name, severity=Severity.MEDIUM, topic=topic,
                t_start=span[0], t_end=span[1],
                summary=f"{topic} stamps with its own publish time, so the trace back to "
                        f"capture is lost here",
                evidence={"origin_fraction": round(origin_frac, 3),
                          "age_p95_ms": round(st.p95.value * 1000, 3),
                          "messages": float(measured)},
                rule=f"stamp-origin fraction >= {d.restamp_fraction} and P95 age "
                     f"<= {d.restamp_max_age_s * 1000:.0f} ms",
                interpretation="A real sensor's capture predates its publish. A stamp "
                               "equal to the publish time is a restamp, so the age of "
                               "the sensor data behind this topic — and behind "
                               "everything downstream of it — cannot be recovered.",
            ))
        return out

    def _trend(self, topic: str, st: _TopicAge, t_end: float) -> list[Finding]:
        d = self.cfg.data_age
        if len(st.bucket_p99) < d.min_buckets:
            return []
        ys = list(st.bucket_p99)
        if max(ys) < d.min_age_s:
            return []
        first = st._bucket_index - len(ys)
        origin = st._t_origin or 0.0
        xs = [origin + (first + i) * d.bucket_s for i in range(len(ys))]

        threshold = d.rel_growth_by_sensitivity[self.cfg.sensitivity]

        # Rule 1 — the ramp is still climbing inside the window.
        slope = theil_sen(xs, ys)  # seconds of age per second of recording
        _tau, p = kendall_tau_p(xs, ys)
        base = ys[0] if ys[0] > 0 else d.min_age_s
        growth = slope * (xs[-1] - xs[0]) / base
        rule = (f"Theil-Sen slope of bucketed P99 age, growth >= {threshold} "
                f"with Kendall tau p <= {d.tau_p_max}")
        fired = growth >= threshold and p <= d.tau_p_max

        # Rule 2 — the ramp finished earlier in the mission and has plateaued. The
        # window has forgotten where it started; the frozen baseline has not. Compared
        # against a *sustained* recent level rather than the latest bucket, so a single
        # slow bucket cannot raise an alarm on its own.
        if not fired and st.baseline_p99 and st.baseline_p99 > 0:
            recent = sorted(ys[-d.min_buckets:])
            sustained = recent[len(recent) // 2]
            baseline_growth = (sustained - st.baseline_p99) / st.baseline_p99
            if baseline_growth >= threshold:
                fired = True
                growth = baseline_growth
                base = st.baseline_p99
                ys = [st.baseline_p99, sustained]
                rule = (f"sustained P99 age vs the mission's opening P99, "
                        f"growth >= {threshold}")

        if not fired:
            return []

        return [Finding(
            detector=self.name,
            severity=Severity.HIGH if growth >= 2 * threshold else Severity.MEDIUM,
            topic=topic,
            t_start=xs[0],
            t_end=xs[-1],
            summary=(
                f"{topic} data age is growing: P99 {ys[0] * 1000:.0f} ms → "
                f"{ys[-1] * 1000:.0f} ms over {xs[-1] - xs[0]:.0f} s"
            ),
            evidence={
                "p99_start_ms": round(ys[0] * 1000, 1),
                "p99_end_ms": round(ys[-1] * 1000, 1),
                "rel_growth": round(growth, 3),
                "slope_ms_per_min": round(slope * 60_000, 2),
                "tau_p": round(p, 4),
                "buckets": float(len(ys)),
            },
            rule=rule,
            interpretation="The tail moves before the mean does. A stage that is falling "
                           "behind shows here well before the robot visibly reacts late.",
        )]

    # -- reporting ---------------------------------------------------------

    def report(self) -> dict[str, Any]:
        """Per-stage and end-to-end ages, plus what could not be measured.

        This is the number teams actually want, so it is a first-class part of the
        report rather than only a finding.
        """
        d = self.cfg.data_age
        stages: list[dict[str, Any]] = []
        for topic, st in sorted(self.topics.items()):
            if not st.p50.count:
                continue
            up = st.upstream(d)
            measured = max(st.n - st.n_unstamped, 1)
            stages.append({
                "topic": topic,
                "upstream": up[0] if up else None,
                "link_observations": up[1] if up else 0,
                "messages": st.n,
                "origin_fraction": round(st.n_origin / measured, 3),
                # A topic too sparse to support a per-bucket P99 gets its age reported
                # and its *trend* refused. Saying which is which here is what stops a
                # silent skip from reading as "checked and healthy".
                "trend_assessable": st.n_buckets_kept >= d.min_buckets,
                "age_p50_ms": round(st.p50.value * 1000, 2),
                "age_p95_ms": round(st.p95.value * 1000, 2),
                "age_p99_ms": round(st.p99.value * 1000, 2),
                "stage_p50_ms": round(st.stage_p50.value * 1000, 2) if up else None,
                "stage_p95_ms": round(st.stage_p95.value * 1000, 2) if up else None,
                "stage_p99_ms": round(st.stage_p99.value * 1000, 2) if up else None,
            })
        # the end of a chain is a topic nothing else claims as an upstream
        claimed = {s["upstream"] for s in stages if s["upstream"]}
        endpoints = [s["topic"] for s in stages if s["topic"] not in claimed]
        return {
            "stages": stages,
            "endpoints": endpoints,
            "unmeasurable": dict(sorted(self.unmeasurable.items())),
            "clock_suspect": self.clock_suspect,
            "stamp_table_evictions": self._evictions,
        }

    # -- checkpoint --------------------------------------------------------

    def state_bytes(self) -> int:
        # 6 P² estimators (10 numbers each) + bucket ring + candidate counts, per topic
        per_topic = 6 * 10 * 8 + self.cfg.data_age.n_buckets * 8 + 8 * 40
        return per_topic * max(len(self.topics), 1) + len(self._stamps) * 48

    def to_state(self) -> dict[str, Any]:
        return {
            "clock_suspect": self.clock_suspect,
            "evictions": self._evictions,
            "unmeasurable": dict(self.unmeasurable),
            "topics": {
                tp: {
                    "p50": st.p50.to_state(), "p95": st.p95.to_state(),
                    "p99": st.p99.to_state(),
                    "stage_p50": st.stage_p50.to_state(),
                    "stage_p95": st.stage_p95.to_state(),
                    "stage_p99": st.stage_p99.to_state(),
                    "n": st.n, "n_unstamped": st.n_unstamped,
                    "n_unset": st.n_unset, "n_ahead": st.n_ahead,
                    "n_origin": st.n_origin, "n_offclock": st.n_offclock,
                    "first_t": st.first_t, "last_t": st.last_t,
                    "bucket_p99": list(st.bucket_p99),
                    "baseline_p99": st.baseline_p99,
                    "baseline_samples": list(st._baseline_samples),
                    "bucket_index": st._bucket_index,
                    "n_buckets_kept": st.n_buckets_kept,
                    "bucket_end": st._bucket_end,
                    "t_origin": st._t_origin,
                    "upstream_counts": dict(st.upstream_counts),
                }
                for tp, st in self.topics.items()
            },
            "stamps": [[k, *v] for k, v in self._stamps.items()],
        }

    @classmethod
    def from_state(cls, state: dict[str, Any], cfg: Config | None = None) -> DataAgeDetector:
        obj = cls(cfg)
        obj.clock_suspect = bool(state.get("clock_suspect", False))
        obj._evictions = int(state.get("evictions", 0))
        obj.unmeasurable = dict(state.get("unmeasurable", {}))
        for tp, s in state.get("topics", {}).items():
            st = _TopicAge()
            st.topic = tp
            st.p50 = P2Quantile.from_state(s["p50"])
            st.p95 = P2Quantile.from_state(s["p95"])
            st.p99 = P2Quantile.from_state(s["p99"])
            st.stage_p50 = P2Quantile.from_state(s["stage_p50"])
            st.stage_p95 = P2Quantile.from_state(s["stage_p95"])
            st.stage_p99 = P2Quantile.from_state(s["stage_p99"])
            st.n = int(s["n"])
            st.n_unstamped = int(s["n_unstamped"])
            st.n_unset = int(s.get("n_unset", 0))
            st.n_ahead = int(s.get("n_ahead", 0))
            st.n_origin = int(s.get("n_origin", 0))
            st.n_offclock = int(s.get("n_offclock", 0))
            st.first_t = s["first_t"]
            st.last_t = float(s["last_t"])
            st.bucket_p99 = deque(float(v) for v in s["bucket_p99"])
            st.baseline_p99 = s.get("baseline_p99")
            st._baseline_samples = [float(v) for v in s.get("baseline_samples", [])]
            st._bucket_index = int(s["bucket_index"])
            st.n_buckets_kept = int(s.get("n_buckets_kept", 0))
            st._bucket_end = s["bucket_end"]
            st._t_origin = s["t_origin"]
            st.upstream_counts = {k: int(v) for k, v in s["upstream_counts"].items()}
            obj.topics[tp] = st
        for row in state.get("stamps", []):
            obj._stamps[int(row[0])] = (row[1], float(row[2]), row[3], float(row[4]))
        return obj
