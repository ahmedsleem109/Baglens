"""Alert semantics: what reaches an operator, and what must not.

The bar these tests hold is not "does it detect the stall" — that is the detector's job
and is measured elsewhere. It is that the *alerting* layer is quiet enough to stay
trusted: one event produces one alert, a healthy recording produces none, and nothing is
ever sent that a later snapshot would take back.
"""

from __future__ import annotations

from pathlib import Path

from baglens.alerts import AlertMonitor, AlertPolicy, stall_coverage_s
from baglens.live import LiveMonitor, ReplayFeed


def _alerts(path: Path, policy: AlertPolicy | None = None, every: int = 200):
    monitor = LiveMonitor(ReplayFeed(path, speed=0))
    alerts = AlertMonitor(policy)
    out = []
    for report in monitor.run(snapshot_every_n=every):
        alert = alerts.update(report)
        if alert is not None:
            out.append(alert)
    return out, alerts


def test_a_clean_recording_raises_nothing(clean_bag: Path) -> None:
    """The whole point. An alert on a healthy recording is how a monitor gets muted."""
    raised, _ = _alerts(clean_bag)
    assert raised == []


def test_one_stall_produces_one_alert(stall_bag: Path) -> None:
    """The fixture stalls once, for six seconds. An operator should hear about it once.

    The verdict crosses its threshold repeatedly over the same event; alerting on stall
    coverage instead means the repeated crossings collapse into the single fact that
    caused them.
    """
    raised, _ = _alerts(stall_bag)
    growth = [a for a in raised if a.kind == "stall_growth"]
    assert len(growth) == 1, [a.message for a in raised]
    assert growth[0].new_stall_s > 5.0
    assert growth[0].severity == "critical"  # six seconds of ninety is well past 5%


def test_a_trivial_attribution_is_not_a_page(dropout_bag: Path) -> None:
    """Below the floor, nothing is sent — whatever the verdict happens to be doing."""
    policy = AlertPolicy(min_new_stall_s=1_000.0)
    raised, _ = _alerts(dropout_bag, policy)
    assert raised == []


def test_alerts_are_never_retracted(stall_bag: Path) -> None:
    """Coverage only grows, so an alert's claim stays true for the rest of the run.

    This is the property the whole design exists for: an operator who acted on the alert
    must not be told later that it did not happen.
    """
    monitor = LiveMonitor(ReplayFeed(stall_bag, speed=0))
    alerts = AlertMonitor()
    claimed = 0.0
    for report in monitor.run(snapshot_every_n=200):
        alerts.update(report)
        covered = stall_coverage_s(report)
        assert covered >= claimed - 1e-9, "an alerted stall stopped being a stall"
        claimed = max(claimed, covered)
    assert claimed > 0


def test_a_continuing_stall_reports_periodically_not_continuously(stall_bag: Path) -> None:
    """A recording that keeps stalling must not produce an alert per snapshot."""
    policy = AlertPolicy(min_new_stall_s=0.01, dwell_s=0.0, urgent_new_stall_s=0.01,
                         min_interval_s=1_000.0)
    raised, _ = _alerts(stall_bag, policy, every=50)
    growth = [a for a in raised if a.kind == "stall_growth"]
    assert len(growth) == 1, f"{len(growth)} alerts for one stall"


def test_the_condition_clears_and_says_so(stall_bag: Path) -> None:
    """An operator learns that it stopped, not only that it started."""
    policy = AlertPolicy(clear_after_s=10.0)
    raised, _ = _alerts(stall_bag, policy)
    kinds = [a.kind for a in raised]
    assert kinds == ["stall_growth", "cleared"], kinds
    assert raised[-1].severity == "info"
    assert raised[-1].total_stall_s == raised[0].total_stall_s
