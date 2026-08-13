"""ROS 2 QoS profile parsing and mismatch detection.

QoS is where silent data loss is *configured*. A BEST_EFFORT sensor topic drops under
load by design; a KEEP_LAST depth-1 queue discards the moment a subscriber stalls; a
declared deadline nobody honours makes every downstream timeout wrong. None of this is
visible in a message count, and all of it is recorded in the bag.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

HISTORY = {0: "system_default", 1: "keep_last", 2: "keep_all", 3: "unknown"}
RELIABILITY = {0: "system_default", 1: "reliable", 2: "best_effort", 3: "unknown"}
DURABILITY = {0: "system_default", 1: "transient_local", 2: "volatile", 3: "unknown"}
LIVELINESS = {0: "system_default", 1: "automatic", 3: "manual_by_topic", 4: "unknown"}

#: rmw writes this sentinel for "no deadline / infinite"
INFINITE_S = 1e6


@dataclass
class QosProfile:
    history: str = "unknown"
    depth: int = 0
    reliability: str = "unknown"
    durability: str = "unknown"
    liveliness: str = "unknown"
    deadline_s: float | None = None
    lifespan_s: float | None = None
    lease_duration_s: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def declared_hz(self) -> float | None:
        return 1.0 / self.deadline_s if self.deadline_s else None


def _duration(node: Any) -> float | None:
    if not isinstance(node, dict):
        return None
    sec = float(node.get("sec") or 0)
    nsec = float(node.get("nsec") or node.get("nanosec") or 0)
    total = sec + nsec / 1e9
    return total if 0.0 < total < INFINITE_S else None


def parse_qos(metadata: dict[str, str]) -> QosProfile | None:
    """Parse the `offered_qos_profiles` blob a rosbag2 recorder writes per channel."""
    raw = metadata.get("offered_qos_profiles") or metadata.get("qos")
    if not raw:
        return None
    try:
        import yaml

        parsed = yaml.safe_load(raw)
    except Exception:
        return None
    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list) or not parsed:
        return None
    prof = next((p for p in parsed if isinstance(p, dict)), None)
    if prof is None:
        return None

    def name(table: dict[int, str], key: str) -> str:
        value = prof.get(key)
        if isinstance(value, str):
            return value.lower()
        try:
            return table.get(int(value), "unknown")  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return "unknown"

    return QosProfile(
        history=name(HISTORY, "history"),
        depth=int(prof.get("depth") or 0),
        reliability=name(RELIABILITY, "reliability"),
        durability=name(DURABILITY, "durability"),
        liveliness=name(LIVELINESS, "liveliness"),
        deadline_s=_duration(prof.get("deadline")),
        lifespan_s=_duration(prof.get("lifespan")),
        lease_duration_s=_duration(prof.get("liveliness_lease_duration")),
        raw=prof,
    )


@dataclass
class QosIssue:
    topic: str
    kind: str
    severity: str  # low | medium | high
    detail: str
    recommendation: str


def check_profile(topic: str, qos: QosProfile, observed_hz: float,
                  drop_rate: float = 0.0) -> list[QosIssue]:
    """Flag the profiles that explain, or predict, silent loss."""
    issues: list[QosIssue] = []

    if qos.reliability == "best_effort":
        issues.append(
            QosIssue(
                topic=topic,
                kind="best_effort",
                severity="high" if drop_rate > 0.02 else "medium",
                detail=(
                    f"{topic} was offered BEST_EFFORT at {observed_hz:.1f} Hz"
                    + (f", and ~{drop_rate * 100:.0f}% of its messages are missing"
                       if drop_rate > 0.02 else "")
                ),
                recommendation=(
                    "BEST_EFFORT permits the middleware to drop under load, and the "
                    "recorder cannot record what it never received. Missing messages on "
                    "this topic are expected behaviour, not evidence of a sensor fault"
                ),
            )
        )

    if qos.history == "keep_last" and 0 < qos.depth <= 5 and observed_hz >= 20:
        issues.append(
            QosIssue(
                topic=topic,
                kind="shallow_queue",
                severity="medium",
                detail=f"{topic} publishes at {observed_hz:.1f} Hz behind a depth-{qos.depth} queue",
                recommendation=(
                    "a queue this shallow at this rate discards messages the moment the "
                    "recorder stalls for a few periods — pair it with the recorder-lag "
                    "curve in health.clock_report before blaming the publisher"
                ),
            )
        )

    if qos.declared_hz and observed_hz > 0:
        ratio = observed_hz / qos.declared_hz
        if ratio < 1 / 3 or ratio > 3:
            issues.append(
                QosIssue(
                    topic=topic,
                    kind="deadline_mismatch",
                    severity="medium",
                    detail=(
                        f"{topic} declares a {qos.declared_hz:.1f} Hz deadline but was "
                        f"recorded at {observed_hz:.1f} Hz"
                    ),
                    recommendation=(
                        "subscribers relying on that deadline will see missed-deadline "
                        "events; either the profile or the publisher is wrong. baglens "
                        "uses the observed rate for its own thresholds"
                    ),
                )
            )

    if qos.durability == "transient_local" and observed_hz >= 20:
        issues.append(
            QosIssue(
                topic=topic,
                kind="transient_local_stream",
                severity="low",
                detail=f"{topic} is TRANSIENT_LOCAL at {observed_hz:.1f} Hz",
                recommendation=(
                    "transient-local durability keeps a history for late joiners, which "
                    "is meant for latched topics like maps, not for a live stream"
                ),
            )
        )

    return issues
