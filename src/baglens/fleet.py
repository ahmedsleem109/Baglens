"""Per-unit tracking: the step from auditing a recording to knowing a vehicle.

Everything here is a SQL question against the catalog. No recording is reopened, which is
the property that makes a fleet answerable at all — and it is why `ingest_landing` must
have run first, with a real `robot_id` rather than a directory name.

Three questions, in the order an operations team actually asks them:

1. **"Is this unit getting worse?"** — `fingerprint`. A single mission's health score is
   noise; six missions with a slope is a maintenance decision. The trend is Theil-Sen
   rather than least squares because one bad mission must not set the direction, and it
   is reported with the spread so a caller can see whether the slope means anything.
2. **"Should this vehicle fly today?"** — `preflight`. The answer must be a decision, not
   a dashboard: `go`, `warn` or `block`, each with the reasons that produced it.
3. **"Has this happened before?"** — `precedents`. The question the README promises, and
   the one that turns a finding into a diagnosis.

A unit with too little history is not a passing unit. Every function here says how much
evidence it had, and `preflight` refuses to return `go` on a unit it has never seen.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median
from typing import Any

from .catalog.store import Catalog

#: below this many missions, a trend is not a trend
MIN_HISTORY = 3


@dataclass
class Trend:
    """A metric's direction for one unit, with the evidence behind it."""

    metric: str
    n: int
    first: float
    last: float
    #: units per mission, Theil-Sen
    slope: float
    #: median absolute deviation of the values — the scale the slope should be read against
    spread: float
    direction: str  # improving | stable | degrading | unknown

    def as_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric, "n": self.n, "first": round(self.first, 4),
            "last": round(self.last, 4), "slope_per_mission": round(self.slope, 5),
            "spread": round(self.spread, 5), "direction": self.direction,
        }


def theil_sen(values: list[float]) -> float:
    """Median pairwise slope against index. Robust: one outlier mission cannot set it."""
    n = len(values)
    if n < 2:
        return 0.0
    slopes = [
        (values[j] - values[i]) / (j - i)
        for i in range(n - 1)
        for j in range(i + 1, n)
    ]
    return median(slopes)


def _mad(values: list[float]) -> float:
    if not values:
        return 0.0
    m = median(values)
    return median([abs(v - m) for v in values])


def _trend(metric: str, values: list[float], higher_is_better: bool) -> Trend:
    n = len(values)
    if n < MIN_HISTORY:
        return Trend(metric, n, values[0] if values else 0.0, values[-1] if values else 0.0,
                     0.0, 0.0, "unknown")
    slope = theil_sen(values)
    spread = _mad(values)
    # A slope only counts as a direction once it would move the metric by more than its
    # own noise across the history in hand. Otherwise every unit trends, always.
    moved = abs(slope) * (n - 1)
    if moved <= max(spread, 1e-9):
        direction = "stable"
    elif (slope > 0) == higher_is_better:
        direction = "improving"
    else:
        direction = "degrading"
    return Trend(metric, n, values[0], values[-1], slope, spread, direction)


@dataclass
class Fingerprint:
    """What this unit looks like over its recorded history."""

    robot_id: str
    missions: int
    trends: list[Trend] = field(default_factory=list)
    #: topics whose observed rate is drifting, worst first
    drifting_topics: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "robot_id": self.robot_id, "missions": self.missions,
            "trends": [t.as_dict() for t in self.trends],
            "drifting_topics": self.drifting_topics, "notes": self.notes,
        }


def unit_missions(cat: Catalog, robot_id: str, limit: int = 50) -> list[dict[str, Any]]:
    """This unit's missions, oldest first — the order a trend has to be read in."""
    rows = cat.query(
        """SELECT mission_id, path, start_time, duration_s, health_score, verdict
           FROM missions WHERE robot_id = ?
           ORDER BY COALESCE(start_time, indexed_at) DESC LIMIT ?""",
        [robot_id, limit],
    )
    return list(reversed(rows))


def fingerprint(cat: Catalog, robot_id: str, limit: int = 50,
                min_topic_history: int = MIN_HISTORY) -> Fingerprint:
    """How this unit has behaved across its recorded missions.

    "SN-0043's IMU noise floor has been climbing for six flights" is `drifting_topics`:
    per-topic jitter and observed rate, tracked across missions rather than within one.
    """
    missions = unit_missions(cat, robot_id, limit)
    fp = Fingerprint(robot_id=robot_id, missions=len(missions))
    if not missions:
        fp.notes.append(f"no missions in the catalog for robot_id={robot_id!r}")
        return fp

    fp.trends = [
        _trend("health_score", [float(m["health_score"] or 0.0) for m in missions], True),
        _trend("duration_s", [float(m["duration_s"] or 0.0) for m in missions], True),
    ]

    ids = [m["mission_id"] for m in missions]
    order = {mid: i for i, mid in enumerate(ids)}
    rows = cat.query(
        f"""SELECT mission_id, topic, actual_hz, jitter_cv, score
            FROM topics WHERE mission_id IN ({",".join("?" * len(ids))})""",
        ids,
    )
    by_topic: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    for r in rows:
        by_topic.setdefault(r["topic"], []).append((order[r["mission_id"]], r))

    drifting = []
    for topic, entries in by_topic.items():
        entries.sort()
        if len(entries) < min_topic_history:
            continue
        for metric, higher_better in (("jitter_cv", False), ("actual_hz", True),
                                      ("score", True)):
            values = [float(r[metric] or 0.0) for _i, r in entries]
            t = _trend(f"{topic}:{metric}", values, higher_better)
            if t.direction == "degrading":
                drifting.append({**t.as_dict(), "topic": topic, "measure": metric})
    # worst first: the largest move relative to the topic's own noise
    drifting.sort(key=lambda d: -abs(d["slope_per_mission"]) / max(d["spread"], 1e-9))
    fp.drifting_topics = drifting[:20]

    if len(missions) < MIN_HISTORY:
        fp.notes.append(
            f"only {len(missions)} mission(s) for this unit; no trend is claimed below "
            f"{MIN_HISTORY}"
        )
    return fp


@dataclass
class PreflightPolicy:
    """What an operations team is willing to fly."""

    #: consider this many of the unit's most recent missions
    lookback: int = 5
    #: block below this health score on the most recent mission
    block_below: float = 60.0
    #: warn below this
    warn_below: float = 85.0
    #: block if this many of the lookback missions were `compromised`
    max_compromised: int = 2
    #: block if the unit's health trend is degrading by more than this per mission
    max_decline_per_mission: float = 2.0


@dataclass
class PreflightDecision:
    robot_id: str
    decision: str  # go | warn | block
    reasons: list[str] = field(default_factory=list)
    missions_considered: int = 0
    last_score: float = 0.0
    trend: Trend | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "robot_id": self.robot_id, "decision": self.decision, "reasons": self.reasons,
            "missions_considered": self.missions_considered,
            "last_score": round(self.last_score, 2),
            "trend": self.trend.as_dict() if self.trend else None,
        }


def preflight(cat: Catalog, robot_id: str,
              policy: PreflightPolicy | None = None) -> PreflightDecision:
    """Should this vehicle fly today?

    Returns a decision and the reasons for it. A unit with no history gets `warn`, never
    `go`: "we have never seen this vehicle" is not the same as "this vehicle is fine",
    and a gate that cannot tell them apart is worse than no gate.
    """
    p = policy or PreflightPolicy()
    missions = unit_missions(cat, robot_id, p.lookback)
    d = PreflightDecision(robot_id=robot_id, decision="go",
                          missions_considered=len(missions))
    if not missions:
        d.decision = "warn"
        d.reasons.append(f"no recorded missions for {robot_id!r} — nothing to judge it on")
        return d

    scores = [float(m["health_score"] or 0.0) for m in missions]
    d.last_score = scores[-1]
    d.trend = _trend("health_score", scores, True)

    if scores[-1] < p.block_below:
        d.decision = "block"
        d.reasons.append(
            f"last mission scored {scores[-1]:.0f}, below the {p.block_below:.0f} floor"
        )
    elif scores[-1] < p.warn_below:
        d.decision = "warn"
        d.reasons.append(
            f"last mission scored {scores[-1]:.0f}, below the {p.warn_below:.0f} mark"
        )

    compromised = sum(1 for m in missions if m["verdict"] == "compromised")
    if compromised >= p.max_compromised:
        d.decision = "block"
        d.reasons.append(
            f"{compromised} of the last {len(missions)} missions were compromised"
        )

    if d.trend.direction == "degrading" and -d.trend.slope > p.max_decline_per_mission:
        if d.decision == "go":
            d.decision = "warn"
        d.reasons.append(
            f"health declining {-d.trend.slope:.1f} points per mission over "
            f"{d.trend.n} missions"
        )

    if len(missions) < MIN_HISTORY:
        if d.decision == "go":
            d.decision = "warn"
        d.reasons.append(
            f"only {len(missions)} mission(s) of history; too little to clear a unit on"
        )
    if not d.reasons:
        d.reasons.append(
            f"{len(missions)} recent missions, last scored {scores[-1]:.0f}, no decline"
        )
    return d


def precedents(cat: Catalog, kind: str, topic: str | None = None,
               exclude_mission: str | None = None, limit: int = 20) -> dict[str, Any]:
    """Has this happened before, and to whom?

    Takes a finding's detector and topic rather than a finding object, so it can be asked
    about a live alert as easily as about a stored one.
    """
    sql = ["SELECT e.mission_id, m.robot_id, m.start_time, e.t, e.severity, e.summary",
           "FROM events e JOIN missions m ON m.mission_id = e.mission_id",
           "WHERE e.kind = ?"]
    params: list[Any] = [kind]
    if topic:
        sql.append("AND e.topic = ?")
        params.append(topic)
    if exclude_mission:
        sql.append("AND e.mission_id != ?")
        params.append(exclude_mission)
    sql.append("ORDER BY m.start_time DESC LIMIT ?")
    params.append(limit)

    rows = cat.query(" ".join(sql), params)
    units = sorted({r["robot_id"] for r in rows if r["robot_id"]})
    return {
        "kind": kind,
        "topic": topic,
        "occurrences": len(rows),
        "units_affected": units,
        "fleet_wide": len(units) > 1,
        "interpretation": (
            f"seen on {len(units)} units — look at what they share rather than at this "
            f"vehicle" if len(units) > 1
            else "seen only on this unit" if units
            else "no precedent in the catalog"
        ),
        "examples": rows[:5],
    }
