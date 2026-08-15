"""Alert semantics — what a monitor is allowed to page an operator about.

The raw verdict is not it. Observed on a real flight replayed at 40x, the verdict went
usable → compromised → usable → compromised → usable, crossing the threshold four times
in nineteen seconds. Neither is "a new finding appeared": findings are legitimately
*revised* as evidence arrives. `aperiodic` withdraws once a topic earns a baseline, and a
`dropped` estimate is corrected the moment the silence behind it is attributed to the
recorder rather than to the sensor. An alert rule built on either would fire and then
retract, and a rule that retracts gets muted — after which the on-vehicle work is dead
weight whether or not the detectors are right.

One quantity is monotonic, and it is the one an operator would act on: **the recording
time attributed to a system-wide stall**. It is tested to never shrink
(`test_live_findings_are_revised_but_stall_coverage_never_shrinks`), because attributing a
window to the recorder is a conclusion that new evidence can extend but not withdraw.

So the rule here is: alert on *growth* in stall coverage, with

* a **floor** (`min_new_stall_s`) so a 40 ms attribution is not a page,
* a **dwell** so one stall produces one alert rather than one per snapshot — the burst is
  allowed to settle before anything is sent, unless it is already large enough that
  waiting would itself be the failure,
* a **minimum interval**, so a recording that is stalling continuously reports
  periodically rather than continuously,
* and an explicit **clear**, so an operator learns that it stopped as well as that it
  started.

Bounded state, single pass, fed one snapshot at a time — the same constraint the detectors
run under, for the same reason: this has to work on the vehicle.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

Severity = Literal["info", "warning", "critical"]


@dataclass(frozen=True)
class AlertPolicy:
    """Thresholds, all in seconds of the stream's own clock."""

    #: growth below this is noise — a stall shorter than this is not worth a page
    min_new_stall_s: float = 0.5
    #: let a burst of growth settle for this long before sending, so one event is one alert
    dwell_s: float = 5.0
    #: send immediately at this much accumulated growth; waiting would be the failure
    urgent_new_stall_s: float = 5.0
    #: never send two alerts closer together than this
    min_interval_s: float = 60.0
    #: after this long with no further growth, the condition is over and a clear is sent
    clear_after_s: float = 30.0
    #: fraction of the stream lost to stalls at which an alert becomes critical
    critical_coverage: float = 0.05


@dataclass
class Alert:
    kind: Literal["stall_growth", "cleared"]
    severity: Severity
    #: stream time the alert was raised at, in seconds from the start of the stream
    t_s: float
    #: stall seconds newly attributed since the last alert
    new_stall_s: float
    #: total stall seconds attributed so far
    total_stall_s: float
    #: total stall seconds as a fraction of the stream seen so far
    coverage: float
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind, "severity": self.severity, "t_s": round(self.t_s, 3),
            "new_stall_s": round(self.new_stall_s, 3),
            "total_stall_s": round(self.total_stall_s, 3),
            "coverage": round(self.coverage, 5), "message": self.message,
        }


def stall_coverage_s(report: Any) -> float:
    """Seconds of the stream currently attributed to a system-wide stall.

    The union of the merged-stall windows, not their sum: `correlation` merges per-topic
    silences into one interval per event, and two of those can still overlap after a
    resume. Double-counting them would make coverage exceed the recording and the alert
    severity meaningless.
    """
    spans = sorted(
        (f.t_start, f.t_end)
        for f in report.findings
        if f.detector == "correlation" and f.topic is None
    )
    total, cursor = 0.0, float("-inf")
    for start, end in spans:
        start = max(start, cursor)
        if end > start:
            total += end - start
            cursor = end
    return total


class AlertMonitor:
    """Turn a stream of snapshots into a stream of alerts.

    Feed it every snapshot; it returns an `Alert` on the ones that warrant sending and
    `None` on the rest::

        alerts = AlertMonitor()
        for report in LiveMonitor(feed).run(snapshot_every_s=1.0):
            alert = alerts.update(report)
            if alert is not None:
                notify(alert)
    """

    def __init__(self, policy: AlertPolicy | None = None) -> None:
        self.policy = policy or AlertPolicy()
        self.covered = 0.0
        self.pending = 0.0
        self.last_growth_t = 0.0
        self.last_alert_t = float("-inf")
        self.firing = False
        #: every alert this monitor has sent, for a caller that wants the history
        self.history: list[Alert] = []

    def update(self, report: Any, now_s: float | None = None) -> Alert | None:
        """Consider one snapshot. Returns an alert to send, or None.

        `now_s` defaults to the stream's own elapsed time rather than the wall clock, so
        the decision is identical whether the feed is a subscription at 1x or a replay at
        40x — the same property that lets the live path be tested against recordings.
        """
        p = self.policy
        t = report.duration_s if now_s is None else now_s

        covered = stall_coverage_s(report)
        growth = covered - self.covered
        if growth > 1e-9:
            self.covered = covered
            self.pending += growth
            self.last_growth_t = t

        quiet_for = t - self.last_growth_t
        coverage = covered / t if t > 0 else 0.0

        if self.pending >= p.min_new_stall_s:
            settled = quiet_for >= p.dwell_s
            urgent = self.pending >= p.urgent_new_stall_s
            spaced = t - self.last_alert_t >= p.min_interval_s
            if (settled or urgent) and (spaced or urgent):
                return self._emit("stall_growth", t, coverage)

        if self.firing and quiet_for >= p.clear_after_s:
            self.firing = False
            alert = Alert(
                kind="cleared", severity="info", t_s=t, new_stall_s=0.0,
                total_stall_s=self.covered, coverage=coverage,
                message=(
                    f"recorder stalls stopped: none in the last {quiet_for:.0f}s, "
                    f"{self.covered:.2f}s attributed in total"
                ),
            )
            self.history.append(alert)
            return alert
        return None

    def _emit(self, kind: Literal["stall_growth"], t: float, coverage: float) -> Alert:
        new = self.pending
        self.pending = 0.0
        self.last_alert_t = t
        self.firing = True
        severity: Severity = (
            "critical" if coverage >= self.policy.critical_coverage else "warning"
        )
        alert = Alert(
            kind=kind, severity=severity, t_s=t, new_stall_s=new,
            total_stall_s=self.covered, coverage=coverage,
            message=(
                f"recorder stalled for a further {new:.2f}s "
                f"({self.covered:.2f}s total, {100 * coverage:.1f}% of the stream). "
                f"Everything went quiet at once, so this is the recorder, the disk, the "
                f"CPU or power — not a sensor."
            ),
        )
        self.history.append(alert)
        return alert
