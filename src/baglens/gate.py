"""`baglens gate` — which episodes are safe to train on, and why the rest are not.

A different artifact for a different buyer. `scripts/quality_gate.py` gates a *build*:
did the recordings this CI job produced get worse than last time? This gates a *dataset*:
of these 4,000 episodes, which ones can a policy be trained on without quietly learning
the wrong thing?

**Why the distinction is worth a separate tool.** Nobody minds a 4% lossy debug bag — you
notice, you shrug, you carry on. A training set is different, because the harm is silent
and delayed. An imitation-learning pipeline assumes its streams are aligned; when the
recorder stalls for 200 ms, nothing errors, the action at *t* is simply paired with an
observation from *t−200 ms*, and the model dutifully learns that. You find out weeks
later, in evaluation, having paid for the training run.

So the output is not a score. It is a **manifest**: a machine-readable decision per
episode with a reason code for every rejection, meant to be committed next to the dataset
and diffed when it changes. The reasons are the point — "rejected: 3.2% of the episode
fell inside a recorder stall" is actionable; "score 61" is not.

    baglens gate ~/data/episodes --out manifest.json
    baglens gate ~/data/episodes --require /observation/joint_states,/action --strict

**Scope, stated honestly.** This audits recordings that carry real timestamps — rosbag2
`.mcap`/`.db3`, ROS 1 `.bag`, PX4 `.ulg`. It deliberately does **not** claim to audit
LeRobot-format datasets: those recompute per-frame timestamps as `frame_index / fps`
during conversion (measured across `lerobot/pusht` and `lerobot/aloha_static_coffee` —
inter-frame deltas vary only by float rounding, ~1e-6 s), which erases every trace of the
timing behaviour these detectors read. A capture-side dropout survives that conversion as
frames that do not exist, with the timestamps closed seamlessly over the hole. Auditing it
would take a different detector family, based on kinematic discontinuity rather than on
arrival timing, and pretending otherwise here would be the exact failure this project
exists to avoid.
"""

from __future__ import annotations

import multiprocessing as mp
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: manifest schema version. Bump when a field's meaning changes, never when one is added.
MANIFEST_VERSION = 1


@dataclass
class GatePolicy:
    """What this dataset requires of an episode. Every field is a documented harm.

    Defaults are deliberately permissive on *score* and strict on *structure*: a low score
    is a judgement call about a recording, while a broken clock or a missing required
    topic is an episode that cannot be used regardless of anyone's opinion.
    """

    #: an episode shorter than this is a fragment, not a demonstration
    min_duration_s: float = 1.0
    #: overall health score below which an episode is rejected. None disables the check.
    min_score: float | None = None
    #: fraction of the episode that may fall inside a system-wide recorder stall. Every
    #: such second is time where the streams kept their timestamps and lost their content.
    max_stall_fraction: float = 0.02
    #: estimated message loss on a required topic, as a fraction of what was expected
    max_drop_fraction: float = 0.05
    #: longest single silence on a required topic, in seconds, regardless of fraction.
    #:
    #: Off by default, and the reason is worth stating: a fraction is the only scale-free
    #: limit, but it is the wrong unit for a demonstration. A 15-second hole in a
    #: 31-minute recording is 0.8% and passes every fraction limit here — and it is also
    #: fifteen seconds during which the robot did something the policy will never see.
    #: This tool cannot know how long your episodes are supposed to be, so set it when you
    #: do: for 30 fps visuomotor data, something under a second.
    max_gap_s: float | None = None
    #: how far the recorder may fall behind the publishers before the episode is refused.
    #: Growing lag means log time and publish time disagree by an amount that changes
    #: through the episode, so *whichever* clock the training pipeline keys on, the pairing
    #: drifts. One second is generous; the detector's own concern threshold is 0.5.
    max_clock_lag_s: float | None = 1.0
    #: topics the episode must contain, and which the two limits above are applied to.
    #: Empty means "apply them to every topic", which is the right default for an unknown
    #: dataset and the wrong one for a dataset you know the schema of.
    require_topics: tuple[str, ...] = ()
    #: refuse episodes the auditor could not assess, rather than accepting them by default
    reject_unassessable: bool = True
    #: refuse episodes whose file is incomplete
    reject_truncated: bool = True
    #: refuse episodes whose timestamps go backwards — time alignment is the one thing a
    #: training pipeline cannot repair after the fact
    reject_non_monotonic: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "min_duration_s": self.min_duration_s,
            "min_score": self.min_score,
            "max_stall_fraction": self.max_stall_fraction,
            "max_drop_fraction": self.max_drop_fraction,
            "max_gap_s": self.max_gap_s,
            "max_clock_lag_s": self.max_clock_lag_s,
            "require_topics": list(self.require_topics),
            "reject_unassessable": self.reject_unassessable,
            "reject_truncated": self.reject_truncated,
            "reject_non_monotonic": self.reject_non_monotonic,
        }


@dataclass
class Episode:
    path: str
    mission_id: str = ""
    duration_s: float = 0.0
    score: float = 0.0
    verdict: str = ""
    confidence: float = 1.0
    decision: str = "accept"  # accept | reject | review
    reasons: list[dict[str, Any]] = field(default_factory=list)
    topics: int = 0
    findings: int = 0

    def reject(self, code: str, detail: str, **evidence: Any) -> None:
        self.decision = "reject"
        self.reasons.append({"code": code, "detail": detail, "evidence": evidence})

    def flag(self, code: str, detail: str, **evidence: Any) -> None:
        """A reason to look, not a reason to drop. Never downgrades an existing reject."""
        if self.decision == "accept":
            self.decision = "review"
        self.reasons.append({"code": code, "detail": detail, "evidence": evidence})


def discover(root: Path) -> list[Path]:
    from .catalog.indexer import BAG_GLOBS

    if root.is_file():
        return [root]
    found: list[Path] = []
    for pattern in BAG_GLOBS:
        found.extend(root.rglob(pattern))
    return sorted({p for p in found if p.is_file()})


def judge(report: Any, policy: GatePolicy) -> Episode:
    """Apply the policy to one audited episode. Pure — no I/O, so it is testable."""
    ep = Episode(
        path=report.path,
        mission_id=report.mission_id,
        duration_s=round(report.duration_s, 3),
        score=round(report.overall_score, 1),
        verdict=report.verdict,
        topics=len(report.topics),
        findings=len(report.findings),
    )
    assessability = getattr(report, "assessability", None)
    if assessability is not None:
        ep.confidence = assessability.confidence

    # 1. Could this episode be judged at all? Asked first, because every check below is
    #    meaningless on a recording the auditor could not assess — and accepting one by
    #    default is how an unaudited episode reaches a training set wearing a clean label.
    if report.verdict == "unassessable":
        detail = (assessability.reasons[0] if assessability and assessability.reasons
                  else "too little of this recording could be measured")
        if policy.reject_unassessable:
            ep.reject("unassessable", detail, confidence=ep.confidence)
        else:
            ep.flag("unassessable", detail, confidence=ep.confidence)

    if report.duration_s < policy.min_duration_s:
        ep.reject("too_short",
                  f"{report.duration_s:.2f}s is shorter than the {policy.min_duration_s:.2f}s "
                  f"floor — a fragment, not a demonstration",
                  duration_s=report.duration_s)

    # 2. Structural failures. These are not opinions about quality.
    fi = report.file_integrity
    if fi is not None and policy.reject_truncated and (fi.truncated_bytes or not fi.readable):
        ep.reject("truncated",
                  "the file is incomplete, so the episode ends before the demonstration "
                  "does — absence of data after that point is not absence of events",
                  truncated_bytes=fi.truncated_bytes, readable=fi.readable)
    if fi is not None and fi.unreadable_fraction > 0.01:
        ep.reject("corrupt",
                  f"{100 * fi.unreadable_fraction:.1f}% of the messages this file claims "
                  f"do not decode",
                  unreadable_fraction=round(fi.unreadable_fraction, 4))

    clock = report.clock
    if clock is not None and policy.reject_non_monotonic and not clock.monotonic:
        ep.reject("clock_non_monotonic",
                  f"timestamps go backwards {clock.backward_jumps}x (worst "
                  f"{clock.max_backward_jump_s:.3f}s) — observation/action alignment "
                  f"cannot be recovered from this episode",
                  backward_jumps=clock.backward_jumps,
                  max_backward_jump_s=clock.max_backward_jump_s)

    # 3. Content loss, checked against the topics that matter to this dataset.
    wanted = set(policy.require_topics)
    present = {t.topic for t in report.topics}
    for topic in sorted(wanted - present):
        ep.reject("missing_topic",
                  f"{topic} is required by this dataset and is not in the episode",
                  topic=topic)

    scored = [t for t in report.topics if not wanted or t.topic in wanted]
    duration = max(report.duration_s, 1e-9)
    worst_stall = max((t.stall_silent_s for t in scored), default=0.0)
    stall_fraction = worst_stall / duration
    if stall_fraction > policy.max_stall_fraction:
        ep.reject("recorder_stall",
                  f"{100 * stall_fraction:.1f}% of the episode fell inside a system-wide "
                  f"recorder stall (limit {100 * policy.max_stall_fraction:.1f}%) — those "
                  f"streams kept their timestamps and lost their content, so anything "
                  f"trained on this window learns a misaligned pair",
                  stall_s=round(worst_stall, 3), fraction=round(stall_fraction, 4))

    if policy.max_gap_s is not None:
        for t in scored:
            if t.max_gap_s > policy.max_gap_s:
                ep.reject("gap",
                          f"{t.topic} was silent for {t.max_gap_s:.2f}s in one stretch "
                          f"(limit {policy.max_gap_s:.2f}s) — whatever the robot did in "
                          f"that window is missing from the demonstration",
                          topic=t.topic, max_gap_s=t.max_gap_s)

    if policy.max_clock_lag_s is not None and clock is not None:
        lag = max(clock.lag_max_s, abs(clock.lag_growth_s))
        if lag > policy.max_clock_lag_s:
            ep.reject("clock_lag",
                      f"the recorder fell {lag:.2f}s behind the publishers (limit "
                      f"{policy.max_clock_lag_s:.2f}s) — log time and publish time "
                      f"disagree by an amount that changes through the episode, so the "
                      f"observation/action pairing drifts whichever clock is used",
                      lag_max_s=clock.lag_max_s, lag_growth_s=clock.lag_growth_s)

    for t in scored:
        expected = t.count + t.estimated_dropped
        if not expected or not t.estimated_dropped:
            continue
        frac = t.estimated_dropped / expected
        if frac > policy.max_drop_fraction:
            ep.reject("message_loss",
                      f"{t.topic} lost an estimated {100 * frac:.1f}% of its messages "
                      f"(limit {100 * policy.max_drop_fraction:.1f}%)",
                      topic=t.topic, fraction=round(frac, 4),
                      estimated_dropped=t.estimated_dropped)

    # 4. The score, last and softest: a judgement rather than a fact about the episode.
    if policy.min_score is not None and report.overall_score < policy.min_score:
        ep.reject("low_score",
                  f"health score {report.overall_score:.1f} is below the {policy.min_score:.1f} "
                  f"floor this dataset requires",
                  score=report.overall_score)
    elif report.verdict == "compromised":
        ep.flag("compromised",
                "the auditor rates this recording compromised; no individual limit was "
                "exceeded, so it is flagged rather than dropped")
    return ep


def _audit_and_judge(args: tuple[str, dict[str, Any]]) -> dict[str, Any]:
    """Worker entry point. Takes plain data so it survives pickling."""
    path, policy_data = args
    from .detectors import Auditor
    from .readers import open_bag

    policy = GatePolicy(**{**policy_data,
                           "require_topics": tuple(policy_data.get("require_topics", ()))})
    try:
        reader = open_bag(path)
        try:
            report = Auditor(reader).run()
        finally:
            reader.close()
    except Exception as exc:  # noqa: BLE001 — an episode that will not open is a rejection
        ep = Episode(path=str(path), verdict="unreadable")
        ep.reject("unreadable", f"{type(exc).__name__}: {exc}")
        return ep.__dict__
    return judge(report, policy).__dict__


def run_gate(root: Path, policy: GatePolicy, workers: int = 1) -> dict[str, Any]:
    """Audit every episode under `root` and return the manifest."""
    paths = discover(root)
    payload = [(str(p), policy.to_dict()) for p in paths]
    if workers > 1 and len(payload) > 1:
        with mp.Pool(workers) as pool:
            episodes = pool.map(_audit_and_judge, payload)
    else:
        episodes = [_audit_and_judge(item) for item in payload]

    accepted = [e for e in episodes if e["decision"] == "accept"]
    review = [e for e in episodes if e["decision"] == "review"]
    rejected = [e for e in episodes if e["decision"] == "reject"]

    by_code: dict[str, int] = {}
    for e in rejected:
        for r in e["reasons"]:
            by_code[r["code"]] = by_code.get(r["code"], 0) + 1

    return {
        "manifest_version": MANIFEST_VERSION,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "root": str(root),
        "policy": policy.to_dict(),
        "summary": {
            "episodes": len(episodes),
            "accepted": len(accepted),
            "review": len(review),
            "rejected": len(rejected),
            "accepted_seconds": round(sum(e["duration_s"] for e in accepted), 1),
            "rejected_seconds": round(sum(e["duration_s"] for e in rejected), 1),
            "rejections_by_code": dict(sorted(by_code.items(), key=lambda kv: -kv[1])),
        },
        # The safe list is emitted separately so a training job can consume it directly
        # without having to re-implement the decision rule and get it subtly wrong.
        "train_on": sorted(e["path"] for e in accepted),
        "episodes": episodes,
    }


def render(manifest: dict[str, Any], verbose: bool = False) -> str:
    s = manifest["summary"]
    lines = [
        f"{s['episodes']} episodes under {manifest['root']}",
        f"  accept {s['accepted']}   review {s['review']}   reject {s['rejected']}",
        f"  {s['accepted_seconds']:.0f}s safe to train on, "
        f"{s['rejected_seconds']:.0f}s withheld",
    ]
    if s["rejections_by_code"]:
        lines.append("  rejections:")
        for code, n in s["rejections_by_code"].items():
            lines.append(f"    {n:>5}  {code}")
    if verbose:
        lines.append("")
        for e in manifest["episodes"]:
            if e["decision"] == "accept":
                continue
            lines.append(f"  {e['decision'].upper():<7} {Path(e['path']).name}")
            for r in e["reasons"]:
                lines.append(f"          {r['code']}: {r['detail']}")
    return "\n".join(lines)
