"""D6 — clock sanity. The highest-value-per-line detector in the set.

Three independent checks:

6a. **Monotonicity.** A timestamp that goes backwards invalidates index-based seeking
    and every time-range query downstream. CRITICAL, counted and located.
6b. **Recorder lag.** ``d(t) = log_time - publish_time``, tracked with an EWMA. A
    *growing* d means the recorder is falling behind the publishers, which is the
    mechanism behind silent drops under load — the well-known rosbag2 complaint about
    dropped frames well below disk throughput. Being able to *show* that curve is the
    point.
6c. **Clock steps.** ``|d(log_time) - d(publish_time)| > 500 ms`` in a single message:
    an NTP correction or a time-sync fight. Backward steps are CRITICAL.

State: 3 floats per topic plus a fixed 200-slot decimating curve buffer.
"""

from __future__ import annotations

from typing import Any

from ..config import CONFIG, Config
from ..models import ClockReport, ClockStep, Finding, Severity
from ..provenance import Provenance
from .base import Ewma


class DecimatingCurve:
    """Bounded downsampler for an unbounded stream: keep <= 2N samples, halve on overflow.

    The offline version would collect everything and resample at the end. That needs
    the end time and the whole array, so it is not allowed here.
    """

    __slots__ = ("cap", "ts", "vs", "stride", "_i")

    def __init__(self, cap: int = 200) -> None:
        self.cap = cap
        self.ts: list[float] = []
        self.vs: list[float] = []
        self.stride = 1
        self._i = 0

    def push(self, t: float, v: float) -> None:
        self._i += 1
        if self._i % self.stride:
            return
        self.ts.append(t)
        self.vs.append(v)
        if len(self.ts) > self.cap:
            self.ts = self.ts[::2]
            self.vs = self.vs[::2]
            self.stride *= 2

    def to_state(self) -> dict[str, Any]:
        # `stride` and `_i` are as much of the state as the samples: they decide when
        # the next halving happens, so a curve restored without them re-densifies and
        # drifts away from the uninterrupted run.
        return {"cap": self.cap, "ts": list(self.ts), "vs": list(self.vs),
                "stride": self.stride, "i": self._i}

    @classmethod
    def from_state(cls, state: dict[str, Any]) -> DecimatingCurve:
        obj = cls(int(state["cap"]))
        obj.ts = [float(x) for x in state["ts"]]
        obj.vs = [float(x) for x in state["vs"]]
        obj.stride = int(state["stride"])
        obj._i = int(state["i"])
        return obj

    def downsample(self, n: int) -> tuple[list[float], list[float]]:
        if len(self.ts) <= n:
            return list(self.ts), list(self.vs)
        step = (len(self.ts) - 1) / (n - 1)
        idx = [int(round(i * step)) for i in range(n)]
        return [self.ts[i] for i in idx], [self.vs[i] for i in idx]


class ClockDetector:
    name = "clock"

    def __init__(self, cfg: Config | None = None) -> None:
        self.cfg = cfg or CONFIG
        c = self.cfg.clock
        self.lag = Ewma(c.lag_ewma_alpha)
        self.curve = DecimatingCurve(200)
        self.lag_start: float | None = None
        self.lag_end = 0.0
        self.lag_max = 0.0
        self.backward_jumps = 0
        self.backward_times: list[float] = []
        self.max_backward = 0.0
        self.steps: list[ClockStep] = []
        self._last: dict[str, tuple[float, float]] = {}  # topic -> (log_t, pub_t)
        self.have_publish_time = False
        self.n = 0
        #: accumulated clock-step correction, so a single NTP jump is not reported
        #: for the rest of the run as "the recorder is 2 seconds behind"
        self._offset = 0.0
        self._last_step_t = -1e18
        self._last_back_t = -1e18

    #: two topics crossing the same step within this window are one event, not two
    EVENT_DEDUPE_S = 1.0

    # -- streaming ---------------------------------------------------------

    def on_arrival(self, topic: str, t: float, pub_t: float) -> None:
        self.n += 1
        prev = self._last.get(topic)
        self._last[topic] = (t, pub_t)

        if prev is not None:
            prev_log, prev_pub = prev

            # 6a — a source clock that goes backwards
            if pub_t < prev_pub and (pub_t - self._last_back_t) > self.EVENT_DEDUPE_S:
                self._last_back_t = pub_t
                self.backward_jumps += 1
                self.max_backward = max(self.max_backward, prev_pub - pub_t)
                if len(self.backward_times) < 100:
                    # the instant the clock *left* — where it jumped back from
                    self.backward_times.append(prev_pub)

            # 6c — log and publish clocks disagreeing about how much time passed.
            # Timed at pub_t: the event happened when the message was published, not
            # when the shifted log clock claims it was written.
            divergence = (t - prev_log) - (pub_t - prev_pub)
            if (
                abs(divergence) > self.cfg.clock.step_s
                and (pub_t - self._last_step_t) > self.EVENT_DEDUPE_S
            ):
                self._last_step_t = pub_t
                self._offset += divergence
                if len(self.steps) < 100:
                    self.steps.append(
                        ClockStep(
                            t=pub_t,
                            delta_s=divergence,
                            direction="forward" if divergence > 0 else "backward",
                            topic=topic,
                        )
                    )

        d = t - pub_t - self._offset
        if abs(t - pub_t) > 1e-9:
            self.have_publish_time = True
        val = self.lag.push(d)
        if self.lag_start is None:
            self.lag_start = val
        self.lag_end = val
        self.lag_max = max(self.lag_max, abs(d))
        self.curve.push(t, d)

    # -- results -----------------------------------------------------------

    @property
    def lag_growth(self) -> float:
        return self.lag_end - (self.lag_start or 0.0)

    def report(self, prov: Provenance | None = None) -> ClockReport:
        ts, vs = self.curve.downsample(self.cfg.clock.lag_curve_points)
        rep = ClockReport(
            monotonic=self.backward_jumps == 0,
            backward_jumps=self.backward_jumps,
            backward_jump_times=[round(t, 4) for t in self.backward_times[:20]],
            max_backward_jump_s=round(self.max_backward, 4),
            lag_curve_t=[round(t, 3) for t in ts],
            lag_curve_s=[round(v, 6) for v in vs],
            lag_start_s=round(self.lag_start or 0.0, 6),
            lag_end_s=round(self.lag_end, 6),
            lag_growth_s=round(self.lag_growth, 6),
            lag_max_s=round(self.lag_max, 6),
            steps=self.steps[:50],
            provenance=prov or Provenance(),
        )
        if self.backward_jumps:
            rep.provenance.warnings.append(
                "the clock ran backwards during this recording — recorder lag is not "
                "measurable across the jump and is not reported"
            )
        if not self.have_publish_time:
            rep.provenance.warnings.append(
                "this format records a single timestamp per message — recorder lag (D6b) "
                "is not measurable and the lag curve is flat by construction, not by health"
            )
        return rep

    def finalize(self, t_end: float) -> list[Finding]:
        out: list[Finding] = []
        c = self.cfg.clock

        if self.backward_jumps:
            out.append(
                Finding(
                    detector="clock",
                    severity=Severity.CRITICAL,
                    t_start=self.backward_times[0] if self.backward_times else 0.0,
                    t_end=self.backward_times[-1] if self.backward_times else t_end,
                    summary=(
                        f"time went backwards {self.backward_jumps}x "
                        f"(worst {self.max_backward * 1000:.0f} ms)"
                    ),
                    evidence={
                        "backward_jumps": float(self.backward_jumps),
                        "max_backward_s": round(self.max_backward, 6),
                    },
                    interpretation=(
                        "index-based seeking and every time-range query over this recording "
                        "are unreliable. Do not compute durations across these instants"
                    ),
                    rule="publish_time monotonicity per topic",
                )
            )

        # Recorder lag is not measurable across a clock that ran backwards: the two
        # time bases interleave and any number we produced would be an artefact of the
        # jump, not of the recorder. Say that instead of reporting a fictional curve.
        if self.have_publish_time and not self.backward_jumps and (
            self.lag_growth > c.lag_growth_s or self.lag_max > c.lag_absolute_s
        ):
            grew = self.lag_growth > c.lag_growth_s
            sev = Severity.HIGH if self.lag_max > 2 * c.lag_absolute_s else Severity.MEDIUM
            out.append(
                Finding(
                    detector="clock_lag",
                    severity=sev,
                    t_start=0.0,
                    t_end=t_end,
                    summary=(
                        f"recorder lag {'grew' if grew else 'reached'} "
                        f"{self.lag_growth * 1000:.0f} ms over the run "
                        f"(peak {self.lag_max * 1000:.0f} ms)"
                    ),
                    evidence={
                        "lag_start_s": round(self.lag_start or 0.0, 6),
                        "lag_end_s": round(self.lag_end, 6),
                        "lag_growth_s": round(self.lag_growth, 6),
                        "lag_max_s": round(self.lag_max, 6),
                    },
                    confidence=0.9,
                    interpretation=(
                        "the recorder is falling behind the publishers. This is the mechanism "
                        "behind silent drops well below disk throughput limits: messages queue, "
                        "the queue overflows, and nothing reports it. Expect diffuse message "
                        "loss in the later part of this recording"
                    ),
                    rule=f"ewma(log_time - publish_time) growth > {c.lag_growth_s}s or peak > {c.lag_absolute_s}s",
                )
            )

        for step in self.steps[:20]:
            if step.direction == "backward" and self.backward_jumps:
                continue  # already reported as a monotonicity failure
            out.append(
                Finding(
                    detector="clock_step",
                    severity=Severity.CRITICAL if step.direction == "backward" else Severity.HIGH,
                    topic=step.topic,
                    t_start=step.t,
                    t_end=step.t,
                    summary=(
                        f"clock step {step.delta_s * 1000:+.0f} ms at t={step.t:.2f}s "
                        f"({step.direction})"
                    ),
                    evidence={"delta_s": round(step.delta_s, 6)},
                    interpretation=(
                        "an NTP correction or a time-sync fight between the recorder host and "
                        "the publisher. Timestamps either side of this instant are not comparable"
                    ),
                    rule=f"|d(log_time) - d(publish_time)| > {c.step_s}s",
                )
            )
        return out

    def state_bytes(self) -> int:
        return 8 * 2 * self.curve.cap + 48 * len(self._last) + 256

    # -- checkpoint --------------------------------------------------------

    def to_state(self) -> dict[str, Any]:
        return {
            "lag": self.lag.to_state(),
            "curve": self.curve.to_state(),
            "lag_start": self.lag_start,
            "lag_end": self.lag_end,
            "lag_max": self.lag_max,
            "backward_jumps": self.backward_jumps,
            "backward_times": list(self.backward_times),
            "max_backward": self.max_backward,
            "steps": [s.model_dump(mode="json") for s in self.steps],
            "last": {k: list(v) for k, v in self._last.items()},
            "have_publish_time": self.have_publish_time,
            "n": self.n,
            "offset": self._offset,
            "last_step_t": self._last_step_t,
            "last_back_t": self._last_back_t,
        }

    @classmethod
    def from_state(cls, state: dict[str, Any], cfg: Config | None = None) -> ClockDetector:
        obj = cls(cfg)
        obj.lag = Ewma.from_state(state["lag"])
        obj.curve = DecimatingCurve.from_state(state["curve"])
        obj.lag_start = state["lag_start"]
        obj.lag_end = float(state["lag_end"])
        obj.lag_max = float(state["lag_max"])
        obj.backward_jumps = int(state["backward_jumps"])
        obj.backward_times = [float(x) for x in state["backward_times"]]
        obj.max_backward = float(state["max_backward"])
        obj.steps = [ClockStep.model_validate(s) for s in state["steps"]]
        obj._last = {k: (float(v[0]), float(v[1])) for k, v in state["last"].items()}
        obj.have_publish_time = bool(state["have_publish_time"])
        obj.n = int(state["n"])
        obj._offset = float(state["offset"])
        obj._last_step_t = float(state["last_step_t"])
        obj._last_back_t = float(state["last_back_t"])
        return obj
