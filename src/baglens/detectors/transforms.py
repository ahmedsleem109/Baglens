"""F3 — transform integrity. The TF failures that waste the most hours, including the
silent ones.

*"Lookup would require extrapolation into the future"* is among the most-cursed errors in
ROS. The loud ones cost an afternoon. The silent ones cost a week: two nodes publishing
the same transform and fighting each other, a static transform nobody ever launched, TF
timestamps ahead of the sensor data they are supposed to align with, a tree that is
complete only intermittently.

`ros2 run tf2_tools view_frames` draws the tree and leaves the diagnosis to you. This
does the diagnosis: every check below produces a named finding with the evidence behind
it, so a human — or an agent — gets *what is wrong* rather than a picture to squint at.

**`/tf` is many streams in one topic.** Each message carries a list of transforms, so the
per-topic cadence machinery has to be applied per parent→child pair. That is more state
than one topic's worth, so it is bounded explicitly at `max_edges` and the truncation is
reported, the way D2 and D7 already do.

**This one really does need a decode.** `tf2_msgs/TFMessage` has no top-level header — it
is a bare sequence — so F1's fixed-offset peek cannot reach the stamps inside it. The cost
is opted into per topic (`reader.decode_topics`) and measured rather than assumed.
"""

from __future__ import annotations

import math
from typing import Any

from ..config import CONFIG, Config
from ..models import Finding, Severity
from .base import Ewma


class _Edge:
    """Everything kept for one parent→child transform. Fixed size."""

    __slots__ = ("parent", "child", "count", "first_t", "last_t", "static", "period",
                 "max_gap_s", "total_silent_s", "n_ahead", "n_dup", "n_dup_disagree",
                 "max_disagreement_m", "last_stamp_ns", "last_pub_ns", "last_xyz",
                 "stamp_lag", "n_stamp_samples")

    def __init__(self, parent: str, child: str) -> None:
        self.parent = parent
        self.child = child
        self.count = 0
        self.first_t: float | None = None
        self.last_t = 0.0
        self.static = False
        self.period = Ewma(0.05)
        self.max_gap_s = 0.0
        self.total_silent_s = 0.0
        #: transforms stamped ahead of the time they were published
        self.n_ahead = 0
        #: the same edge published twice for one stamp — two broadcasters, or one
        #: publishing twice. Either way a consumer sees a transform that will not hold still.
        self.n_dup = 0
        self.n_dup_disagree = 0
        self.max_disagreement_m = 0.0
        self.last_stamp_ns: int | None = None
        self.last_pub_ns: int | None = None
        self.last_xyz: tuple[float, float, float] | None = None
        #: publish time minus stamp — how old a transform is when it is broadcast
        self.stamp_lag = Ewma(0.05)
        self.n_stamp_samples = 0

    @property
    def hz(self) -> float:
        return 1.0 / self.period.value if self.period.value > 0 else 0.0


class TransformDetector:
    """F3. Fed decoded `/tf` and `/tf_static` messages; everything else it learns from
    arrivals it is already seeing."""

    name = "transforms"

    def __init__(self, cfg: Config | None = None) -> None:
        self.cfg = cfg or CONFIG
        t = self.cfg.transforms
        self.edges: dict[tuple[str, str], _Edge] = {}
        self._max_edges = t.max_edges
        self._extrap_tol_ns = int(t.extrapolation_tolerance_s * 1e9)
        self._dropped_edges = 0
        #: frames a *sensor* claims to publish in, from the peeked header. A frame used by
        #: a sensor but never provided by any transform is a transform nobody launched —
        #: which is undetectable from `/tf` alone, because absence has no signal.
        self.consumer_frames: dict[str, str] = {}
        #: newest transform stamp seen so far, for extrapolation risk
        self._newest_tf_stamp_ns: int | None = None
        #: when a transform last arrived, so extrapolation is only judged while TF is live
        self._last_tf_t: float | None = None
        #: consumer topic -> [messages, times its stamp was newer than any transform]
        self.extrapolation: dict[str, list[int]] = {}
        #: bucketed completeness: how many buckets each edge was present in
        self._bucket_s = t.bucket_s
        self._bucket_end: float | None = None
        self._buckets = 0
        self._present_this_bucket: set[tuple[str, str]] = set()
        self._bucket_hits: dict[tuple[str, str], int] = {}
        self.t_end = 0.0
        #: set by the auditor: /tf_static exists in the topic table but carries no
        #: messages, which changes an orphaned frame from a robot fault to a recording one
        self.static_declared_but_empty = False

    # -- streaming ---------------------------------------------------------

    def on_arrival(self, topic: str, t: float, pub_t_ns: int, arrival: Any) -> None:
        """One arrival. TF topics carry a decoded message; everything else contributes
        only its frame_id, which costs nothing extra to look at."""
        self.t_end = max(self.t_end, t)
        self._roll_bucket(t)

        decoded = getattr(arrival, "decoded", None)
        if decoded is not None and hasattr(decoded, "transforms"):
            self._on_tf(topic, t, pub_t_ns, decoded)
            return

        frame = getattr(arrival, "frame_id", None)
        # An empty frame_id is not a frame. `/rosout` carries a Header it never fills in,
        # and reporting '' as an unprovided frame is a finding about nothing.
        if frame and frame.strip():
            self.consumer_frames.setdefault(topic, frame)
        stamp = getattr(arrival, "stamp_ns", None)
        # Only while transforms are actually flowing. On a real recording where `/tf`
        # stopped after 40 s, every later message is trivially "newer than the tree" —
        # which produced 29 findings all restating one fact, that TF had stopped. The
        # outage is the finding; the intermittency check owns it.
        live = (
            self._last_tf_t is not None
            and (t - self._last_tf_t) <= self.cfg.transforms.tf_live_window_s
        )
        if stamp and self._newest_tf_stamp_ns is not None and live:
            row = self.extrapolation.setdefault(topic, [0, 0])
            row[0] += 1
            # a lookup at this stamp would fall past the end of the TF buffer by more
            # than one transform cycle — closer than that and tf2 simply waits
            if stamp - self._newest_tf_stamp_ns > self._extrap_tol_ns:
                row[1] += 1

    def _on_tf(self, topic: str, t: float, pub_t_ns: int, msg: Any) -> None:
        static = topic.endswith("_static")
        if not static:
            self._last_tf_t = t
        for tr in getattr(msg, "transforms", None) or ():
            header = getattr(tr, "header", None)
            parent = str(getattr(header, "frame_id", "") or "")
            child = str(getattr(tr, "child_frame_id", "") or "")
            if not parent or not child:
                continue
            key = (parent, child)
            edge = self.edges.get(key)
            if edge is None:
                if len(self.edges) >= self._max_edges:
                    self._dropped_edges += 1
                    continue
                edge = self.edges[key] = _Edge(parent, child)
                edge.first_t = t
            edge.static = edge.static or static

            stamp = getattr(getattr(header, "stamp", None), "sec", None)
            stamp_ns: int | None = None
            if stamp is not None:
                stamp_ns = int(stamp) * 1_000_000_000 + int(
                    getattr(header.stamp, "nanosec", 0)
                )

            xyz = _xyz(tr)

            # -- duplicate publisher: the same edge, the same stamp, published twice.
            # The disagreement is what separates two broadcasters fighting from one
            # broadcaster that simply repeats itself.
            if stamp_ns is not None and stamp_ns == edge.last_stamp_ns:
                edge.n_dup += 1
                if xyz and edge.last_xyz:
                    d = math.dist(xyz, edge.last_xyz)
                    if d > self.cfg.transforms.disagreement_m:
                        edge.n_dup_disagree += 1
                        edge.max_disagreement_m = max(edge.max_disagreement_m, d)
            elif edge.count:
                dt = t - edge.last_t
                if dt > 0:
                    edge.period.push(dt)
                    if edge.period.value > 0 and dt > self.cfg.transforms.gap_factor * edge.period.value:
                        edge.max_gap_s = max(edge.max_gap_s, dt)
                        edge.total_silent_s += dt

            if stamp_ns is not None:
                lag_s = (pub_t_ns - stamp_ns) / 1e9
                if lag_s < 0:
                    edge.n_ahead += 1
                edge.stamp_lag.push(lag_s)
                edge.n_stamp_samples += 1
                if self._newest_tf_stamp_ns is None or stamp_ns > self._newest_tf_stamp_ns:
                    self._newest_tf_stamp_ns = stamp_ns
                edge.last_stamp_ns = stamp_ns

            edge.count += 1
            edge.last_t = t
            edge.last_pub_ns = pub_t_ns
            edge.last_xyz = xyz
            self._present_this_bucket.add(key)

    def _roll_bucket(self, t: float) -> None:
        if self._bucket_end is None:
            self._bucket_end = t + self._bucket_s
            return
        while t >= self._bucket_end:
            for key in self._present_this_bucket:
                self._bucket_hits[key] = self._bucket_hits.get(key, 0) + 1
            self._present_this_bucket = set()
            self._buckets += 1
            self._bucket_end += self._bucket_s

    # -- structure ---------------------------------------------------------

    def roots(self) -> list[str]:
        children = {c for _p, c in self.edges}
        parents = {p for p, _c in self.edges}
        return sorted(parents - children)

    def provided_frames(self) -> set[str]:
        """Every frame the tree can actually resolve: a child of some transform, or a root."""
        return {c for _p, c in self.edges} | set(self.roots())

    def orphan_frames(self) -> dict[str, str]:
        """Frames a sensor publishes in that no transform provides. `{frame: topic}`.

        Empty when there is no tree at all. "Nothing provides this frame" is only a claim
        you can make against a tree — on a recording with no `/tf`, it would fire on every
        sensor at once and say nothing except that TF was not recorded.
        """
        if not self.edges:
            return {}
        provided = self.provided_frames()
        return {
            frame: topic
            for topic, frame in sorted(self.consumer_frames.items())
            if frame not in provided
        }

    # -- close -------------------------------------------------------------

    def finalize(self, t_end: float) -> list[Finding]:
        c = self.cfg.transforms
        out: list[Finding] = []
        span = max(t_end, self.t_end, 1e-9)

        for (parent, child), e in sorted(self.edges.items()):
            label = f"{parent}→{child}"

            if e.n_dup_disagree >= c.min_duplicate_observations:
                out.append(Finding(
                    detector=self.name, severity=Severity.HIGH, topic="/tf",
                    t_start=e.first_t or 0.0, t_end=e.last_t,
                    summary=f"{label} is published by more than one source, disagreeing "
                            f"by up to {e.max_disagreement_m:.2f} m",
                    evidence={"duplicate_stamps": float(e.n_dup),
                              "disagreements": float(e.n_dup_disagree),
                              "max_disagreement_m": round(e.max_disagreement_m, 4)},
                    rule=f"same parent→child at one stamp, differing by more than "
                         f"{c.disagreement_m} m, at least "
                         f"{c.min_duplicate_observations} times",
                    interpretation="Two nodes are fighting over one transform. Consumers "
                                   "see the pose flip between them depending on which "
                                   "arrived last, and nothing in ROS reports it.",
                ))

            if e.n_stamp_samples and e.n_ahead / e.n_stamp_samples >= c.ahead_fraction:
                out.append(Finding(
                    detector=self.name, severity=Severity.HIGH, topic="/tf",
                    t_start=e.first_t or 0.0, t_end=e.last_t,
                    summary=f"{label} is stamped into the future by "
                            f"{-e.stamp_lag.value * 1000:.0f} ms",
                    evidence={"ahead_fraction": round(e.n_ahead / e.n_stamp_samples, 3),
                              "mean_ahead_ms": round(-e.stamp_lag.value * 1000, 1)},
                    rule=f"transform stamp after its publish time in >= "
                         f"{c.ahead_fraction:.0%} of messages",
                    interpretation="Any lookup at a sensor's stamp lands before this "
                                   "transform's stamp, which is the classic "
                                   "'extrapolation into the past' complaint; and when it "
                                   "stops, everything using it silently freezes.",
                ))

            # intermittent completeness — only meaningful for a transform that is
            # supposed to be continuous, so static edges are exempt by construction
            if not e.static and self._buckets >= c.min_buckets:
                hits = self._bucket_hits.get((parent, child), 0)
                present = hits / self._buckets
                if present < c.min_presence:
                    out.append(Finding(
                        detector=self.name, severity=Severity.MEDIUM, topic="/tf",
                        t_start=e.first_t or 0.0, t_end=e.last_t,
                        summary=f"{label} exists only {present:.0%} of the time "
                                f"(longest gap {e.max_gap_s:.1f}s)",
                        evidence={"present_fraction": round(present, 3),
                                  "max_gap_s": round(e.max_gap_s, 3),
                                  "silent_s": round(e.total_silent_s, 3),
                                  "hz": round(e.hz, 2)},
                        rule=f"present in < {c.min_presence:.0%} of "
                             f"{self._bucket_s:.0f}s buckets",
                        interpretation="Every lookup during the missing windows either "
                                       "fails or silently returns a stale pose. This is "
                                       "the fault that looks like intermittent "
                                       "localisation and is blamed on the sensor.",
                    ))

        orphans = self.orphan_frames()
        if orphans:
            # One finding, not one per frame: a robot whose `/tf_static` is missing has
            # every mounted sensor orphaned at once, and eight findings saying the same
            # thing bury the one sentence that explains them.
            listed = ", ".join(f"'{f}' ({t})" for f, t in list(orphans.items())[:6])
            if self.static_declared_but_empty:
                # Worth separating: the transforms may exist on the robot and simply not
                # be in the file. `/tf_static` is latched, so a recorder that subscribed
                # late records nothing — that is a recording fault, not a robot fault,
                # and calling it the wrong one sends someone to the wrong place.
                out.append(Finding(
                    detector=self.name, severity=Severity.MEDIUM, topic="/tf_static",
                    t_start=0.0, t_end=span,
                    summary=f"/tf_static was recorded but is empty, so {len(orphans)} "
                            f"sensor frame(s) have no transform",
                    evidence={"orphan_frames": float(len(orphans))},
                    rule="frames referenced by publishers but provided by nothing, while "
                         "a declared /tf_static carries zero messages",
                    interpretation="Most likely the recorder subscribed after the latched "
                                   "static transforms were published, so they are missing "
                                   "from the file rather than from the robot. Re-record "
                                   "with the recorder started first to tell the two apart.",
                ))
            else:
                out.append(Finding(
                    detector=self.name, severity=Severity.HIGH, topic="/tf",
                    t_start=0.0, t_end=span,
                    summary=f"{len(orphans)} frame(s) are published in but provided by no "
                            f"transform: {listed}",
                    evidence={"orphan_frames": float(len(orphans)),
                              "frames_in_tree": float(len(self.provided_frames()))},
                    rule="a frame referenced by a publisher but never a child of any "
                         "transform",
                    interpretation="Usually a static transform that was never launched. "
                                   "Nothing can place this sensor's data in the world, and "
                                   "because the transform never existed there is no rate "
                                   "to go missing — which is why it is so easy to miss.",
                ))

        # Rolled up into one finding rather than one per topic. Extrapolation risk is a
        # property of the *tree*, and a real recording has dozens of consumers — thirty
        # findings restating one fact is how a useful signal becomes noise someone mutes.
        at_risk = {
            topic: ahead / n
            for topic, (n, ahead) in sorted(self.extrapolation.items())
            if n >= c.min_extrapolation_samples and ahead / n >= c.extrapolation_fraction
        }
        if at_risk:
            worst = max(at_risk, key=lambda k: at_risk[k])
            out.append(Finding(
                detector=self.name, severity=Severity.MEDIUM, topic="/tf",
                t_start=0.0, t_end=span,
                summary=f"{len(at_risk)} topic(s) publish data newer than the transform "
                        f"tree, worst {worst} at {at_risk[worst]:.0%}",
                evidence={"topics": float(len(at_risk)),
                          "worst_fraction": round(at_risk[worst], 3),
                          **{f"pct:{t}": round(v, 3) for t, v in list(at_risk.items())[:20]}},
                rule=f"while transforms were flowing, sensor stamp newer than the whole "
                     f"tree by more than {c.extrapolation_tolerance_s * 1000:.0f} ms, in "
                     f">= {c.extrapolation_fraction:.0%} of messages",
                interpretation="A lookup at this data's stamp would require extrapolation "
                               "into the future — the error everyone recognises, reported "
                               "before it becomes one. Usually the transform publisher is "
                               "slower than the sensors that depend on it.",
            ))

        if self._dropped_edges:
            out.append(Finding(
                detector=self.name, severity=Severity.INFO, topic="/tf",
                t_start=0.0, t_end=span,
                summary=f"transform table full at {self._max_edges} edges; "
                        f"{self._dropped_edges} were not tracked",
                evidence={"dropped": float(self._dropped_edges),
                          "capacity": float(self._max_edges)},
                rule=f"bounded transform table of {self._max_edges} edges",
                interpretation="Bounded state, reported rather than hidden. Raise "
                               "transforms.max_edges if this tree is genuinely larger.",
            ))
        return out

    # -- reporting ---------------------------------------------------------

    def report(self) -> dict[str, Any]:
        buckets = max(self._buckets, 1)
        return {
            "roots": self.roots(),
            "edges": [
                {
                    "parent": e.parent, "child": e.child, "count": e.count,
                    "hz": round(e.hz, 3), "static": e.static,
                    "max_gap_s": round(e.max_gap_s, 3),
                    "present_fraction": round(
                        self._bucket_hits.get((e.parent, e.child), 0) / buckets, 3
                    ) if not e.static else 1.0,
                    "mean_stamp_lag_ms": round(e.stamp_lag.value * 1000, 2),
                    "duplicate_stamps": e.n_dup,
                    "disagreements": e.n_dup_disagree,
                    "max_disagreement_m": round(e.max_disagreement_m, 4),
                }
                for _k, e in sorted(self.edges.items())
            ],
            "orphan_frames": self.orphan_frames(),
            "consumer_frames": dict(sorted(self.consumer_frames.items())),
            "dropped_edges": self._dropped_edges,
        }

    def state_bytes(self) -> int:
        return 200 * max(len(self.edges), 1) + 64 * len(self.consumer_frames)

    # -- checkpoint --------------------------------------------------------

    def to_state(self) -> dict[str, Any]:
        return {
            "dropped_edges": self._dropped_edges,
            "consumer_frames": dict(self.consumer_frames),
            "newest_tf_stamp_ns": self._newest_tf_stamp_ns,
            "last_tf_t": self._last_tf_t,
            "extrapolation": {k: list(v) for k, v in self.extrapolation.items()},
            "bucket_end": self._bucket_end,
            "buckets": self._buckets,
            "present": [list(k) for k in self._present_this_bucket],
            "bucket_hits": [[list(k), v] for k, v in self._bucket_hits.items()],
            "t_end": self.t_end,
            "static_declared_but_empty": self.static_declared_but_empty,
            "edges": [
                {
                    "parent": e.parent, "child": e.child, "count": e.count,
                    "first_t": e.first_t, "last_t": e.last_t, "static": e.static,
                    "period": e.period.to_state(), "max_gap_s": e.max_gap_s,
                    "total_silent_s": e.total_silent_s, "n_ahead": e.n_ahead,
                    "n_dup": e.n_dup, "n_dup_disagree": e.n_dup_disagree,
                    "max_disagreement_m": e.max_disagreement_m,
                    "last_stamp_ns": e.last_stamp_ns, "last_pub_ns": e.last_pub_ns,
                    "last_xyz": list(e.last_xyz) if e.last_xyz else None,
                    "stamp_lag": e.stamp_lag.to_state(),
                    "n_stamp_samples": e.n_stamp_samples,
                }
                for _k, e in sorted(self.edges.items())
            ],
        }

    @classmethod
    def from_state(cls, state: dict[str, Any],
                   cfg: Config | None = None) -> TransformDetector:
        obj = cls(cfg)
        obj._dropped_edges = int(state.get("dropped_edges", 0))
        obj.consumer_frames = dict(state.get("consumer_frames", {}))
        obj._newest_tf_stamp_ns = state.get("newest_tf_stamp_ns")
        obj._last_tf_t = state.get("last_tf_t")
        obj.extrapolation = {k: list(v) for k, v in state.get("extrapolation", {}).items()}
        obj._bucket_end = state.get("bucket_end")
        obj._buckets = int(state.get("buckets", 0))
        obj._present_this_bucket = {tuple(k) for k in state.get("present", [])}
        obj._bucket_hits = {tuple(k): int(v) for k, v in state.get("bucket_hits", [])}
        obj.t_end = float(state.get("t_end", 0.0))
        obj.static_declared_but_empty = bool(state.get("static_declared_but_empty", False))
        for row in state.get("edges", []):
            e = _Edge(row["parent"], row["child"])
            e.count = int(row["count"])
            e.first_t = row["first_t"]
            e.last_t = float(row["last_t"])
            e.static = bool(row["static"])
            e.period = Ewma.from_state(row["period"])
            e.max_gap_s = float(row["max_gap_s"])
            e.total_silent_s = float(row["total_silent_s"])
            e.n_ahead = int(row["n_ahead"])
            e.n_dup = int(row["n_dup"])
            e.n_dup_disagree = int(row["n_dup_disagree"])
            e.max_disagreement_m = float(row["max_disagreement_m"])
            e.last_stamp_ns = row["last_stamp_ns"]
            e.last_pub_ns = row["last_pub_ns"]
            e.last_xyz = tuple(row["last_xyz"]) if row["last_xyz"] else None
            e.stamp_lag = Ewma.from_state(row["stamp_lag"])
            e.n_stamp_samples = int(row["n_stamp_samples"])
            obj.edges[(e.parent, e.child)] = e
        return obj


def _xyz(tr: Any) -> tuple[float, float, float] | None:
    t = getattr(getattr(tr, "transform", None), "translation", None)
    if t is None:
        return None
    return float(getattr(t, "x", 0.0)), float(getattr(t, "y", 0.0)), float(getattr(t, "z", 0.0))
