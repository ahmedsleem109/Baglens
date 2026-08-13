"""`compare.*` — cross-mission. This is what makes it a fleet tool.

Real debugging is comparative: "this mission failed, the other 200 didn't — what's
different?" Every answer here is computed from the catalog and the Parquet signal
cache, so comparing 200 missions costs no bag reads at all.
"""

from __future__ import annotations

from typing import Any, Literal

import numpy as np
from pydantic import BaseModel, Field

from ..budget import apply_budget
from ..kernels.compare import (
    build_features,
    diff_signals,
    dtw_rank,
    event_anchor,
    load_signal,
    normalise_matrix,
    similarity,
)
from ..models import Budgeted
from ..provenance import Provenance
from .catalog_tools import catalog


class SignalDiffModel(BaseModel):
    signal_key: str
    mean_a: float
    mean_b: float
    delta: float
    percent_change: float
    cohens_d: float
    ks: float
    magnitude: str


class MissionComparison(Budgeted):
    mission_a: str
    mission_b: str
    align: str
    #: for align="event": the instant each mission was anchored on, and the signal used
    anchor_a_s: float = 0.0
    anchor_b_s: float = 0.0
    anchor_signal: str = ""
    most_changed: list[SignalDiffModel] = Field(default_factory=list)
    topics_only_in_a: list[str] = Field(default_factory=list)
    topics_only_in_b: list[str] = Field(default_factory=list)
    health_a: float = 0.0
    health_b: float = 0.0
    verdict: str = ""
    provenance: Provenance = Field(default_factory=Provenance)


class CohortStat(BaseModel):
    cohort: str
    missions: int
    mean: float
    std: float
    p50: float


class CohortComparison(BaseModel):
    metric: str
    split_by: str
    cohorts: list[CohortStat] = Field(default_factory=list)
    cohens_d: float | None = None
    ks: float | None = None
    verdict: str = ""
    provenance: Provenance = Field(default_factory=Provenance)


class SimilarMission(BaseModel):
    mission_id: str
    path: str = ""
    score: float
    dtw_distance: float | None = None
    detail: dict[str, float] = Field(default_factory=dict)
    why: str = ""


class SimilarityResult(BaseModel):
    target: str
    matches: list[SimilarMission] = Field(default_factory=list)
    method: str = ""
    provenance: Provenance = Field(default_factory=Provenance)


class RegressionRow(BaseModel):
    metric: str
    slope_per_day: float
    direction: str
    n_missions: int
    first_value: float
    last_value: float
    concern: str


class RegressionScan(BaseModel):
    rows: list[RegressionRow] = Field(default_factory=list)
    window_days: float = 0.0
    provenance: Provenance = Field(default_factory=Provenance)


class RankedMission(BaseModel):
    mission_id: str
    path: str
    value: float
    health_score: float
    verdict: str


class Ranking(Budgeted):
    metric: str
    ascending: bool
    missions: list[RankedMission] = Field(default_factory=list)
    provenance: Provenance = Field(default_factory=Provenance)


METRICS = {
    "health_score": "m.health_score",
    "duration_s": "m.duration_s",
    "message_count": "m.message_count",
    "size_bytes": "m.size_bytes",
    "gap_count": "(SELECT COALESCE(SUM(gap_count),0) FROM topics t WHERE t.mission_id = m.mission_id)",
    "estimated_dropped": "(SELECT COALESCE(SUM(estimated_dropped),0) FROM topics t WHERE t.mission_id = m.mission_id)",
    "max_jitter_cv": "(SELECT COALESCE(MAX(jitter_cv),0) FROM topics t WHERE t.mission_id = m.mission_id)",
    "event_count": "(SELECT COUNT(*) FROM events e WHERE e.mission_id = m.mission_id)",
    "critical_events": "(SELECT COUNT(*) FROM events e WHERE e.mission_id = m.mission_id AND e.severity >= 4)",
}


def register(mcp: Any) -> None:
    @mcp.tool(name="compare.missions")
    def compare_missions(
        mission_a: str,
        mission_b: str,
        align: Literal["absolute", "progress", "event"] = "progress",
        signals: list[str] | None = None,
        top_k: int = 10,
        anchor_signal: str | None = None,
    ) -> MissionComparison:
        """Diff two missions signal by signal, ranked by effect size.

        `align` matters: "progress" normalises each mission to 0–1 of its own duration
        (the right default when runs differ in length), "absolute" compares raw seconds,
        and "event" anchors each mission on the first instant it actually moved — use
        that when runs idle for different lengths before starting, since otherwise every
        later difference is an artefact of the offset. The anchor instants are reported.

        Returns effect sizes and the most-changed list, never raw data.
        """
        cat = catalog()
        rows = cat.query(
            "SELECT mission_id, path, health_score, verdict FROM missions WHERE mission_id IN (?, ?)",
            [mission_a, mission_b],
        )
        info = {r["mission_id"]: r for r in rows}
        keys_a = {r["signal_key"] for r in cat.query(
            "SELECT signal_key FROM signals WHERE mission_id = ?", [mission_a])}
        keys_b = {r["signal_key"] for r in cat.query(
            "SELECT signal_key FROM signals WHERE mission_id = ?", [mission_b])}
        shared = sorted(set(signals) & keys_a & keys_b) if signals else sorted(keys_a & keys_b)

        anchor_a = anchor_b = 0.0
        anchor_used = ""
        if align == "event":
            anchor_a, source_a = event_anchor(cat, mission_a, anchor_signal)
            anchor_b, source_b = event_anchor(cat, mission_b, anchor_signal)
            anchor_used = source_a or source_b

        diffs = []
        for key in shared:
            sa = load_signal(cat, mission_a, key)
            sb = load_signal(cat, mission_b, key)
            if sa is None or sb is None:
                continue
            _ta, va = sa.aligned(align, anchor_a)
            _tb, vb = sb.aligned(align, anchor_b)
            diffs.append(diff_signals(va, vb, key))
        diffs.sort(key=lambda d: -d.rank)

        topics_a = {r["topic"] for r in cat.query(
            "SELECT topic FROM topics WHERE mission_id = ?", [mission_a])}
        topics_b = {r["topic"] for r in cat.query(
            "SELECT topic FROM topics WHERE mission_id = ?", [mission_b])}

        largest = diffs[0] if diffs else None
        verdict = (
            f"largest change: {largest.signal_key} {largest.percent_change:+.1f}% "
            f"({largest.magnitude} effect, d={largest.cohens_d})"
            if largest
            else "no shared cached signals to compare — index both missions with signals enabled"
        )
        if align == "event" and not anchor_used:
            verdict += (
                " — WARNING: no motion signal was found to anchor on, so this comparison "
                "fell back to absolute time"
            )
        result = MissionComparison(
            mission_a=mission_a,
            mission_b=mission_b,
            align=align,
            anchor_a_s=round(anchor_a, 3),
            anchor_b_s=round(anchor_b, 3),
            anchor_signal=anchor_used,
            most_changed=[SignalDiffModel(**d.__dict__) for d in diffs[:top_k]],
            topics_only_in_a=sorted(topics_a - topics_b)[:20],
            topics_only_in_b=sorted(topics_b - topics_a)[:20],
            health_a=info.get(mission_a, {}).get("health_score", 0.0) or 0.0,
            health_b=info.get(mission_b, {}).get("health_score", 0.0) or 0.0,
            verdict=verdict,
            provenance=Provenance(
                mission_id=mission_a,
                topics=sorted({k.split(".")[0] for k in shared}),
                method=f"effect_sizes(align={align})",
                sample_count=len(shared),
            ),
        )

        def fewer(r: MissionComparison) -> MissionComparison:
            r.most_changed = r.most_changed[: max(3, len(r.most_changed) // 2)]
            return r

        return apply_budget(result, ladder=(fewer, fewer),
                            narrowing="pass signals=[...] to compare a specific set")

    @mcp.tool(name="compare.cohorts")
    def compare_cohorts(
        split_by: Literal["tag", "robot_id", "verdict", "date"],
        metric: str = "health_score",
        value_a: str | None = None,
        value_b: str | None = None,
        split_date: str | None = None,
    ) -> CohortComparison:
        """Split the corpus and compare a metric across the halves. "What changed after v2.4?"

        `split_by="tag"` with value_a/value_b compares two labelled groups;
        `split_by="date"` with `split_date` (YYYY-MM-DD) compares before and after a
        boundary. Metrics: health_score, gap_count, estimated_dropped, max_jitter_cv,
        event_count, critical_events, duration_s, message_count.
        """
        cat = catalog()
        expr = METRICS.get(metric, METRICS["health_score"])
        groups: dict[str, list[float]] = {}

        if split_by == "date" and split_date:
            for label, op in (("before", "<"), ("after", ">=")):
                rows = cat.query(
                    f"SELECT {expr} AS v FROM missions m WHERE m.start_time {op} ?", [split_date]
                )
                groups[f"{label} {split_date}"] = [float(r["v"] or 0) for r in rows]
        elif split_by == "tag":
            for value in [v for v in (value_a, value_b) if v]:
                rows = cat.query(
                    f"""SELECT {expr} AS v FROM missions m
                        WHERE EXISTS (SELECT 1 FROM tags t WHERE t.mission_id = m.mission_id
                                      AND t.tag = ?)""",
                    [value],
                )
                groups[value] = [float(r["v"] or 0) for r in rows]
        else:
            column = "robot_id" if split_by == "robot_id" else "verdict"
            rows = cat.query(f"SELECT m.{column} AS g, {expr} AS v FROM missions m")
            for r in rows:
                groups.setdefault(str(r["g"]), []).append(float(r["v"] or 0))

        stats = [
            CohortStat(
                cohort=name,
                missions=len(vals),
                mean=round(float(np.mean(vals)), 4) if vals else 0.0,
                std=round(float(np.std(vals, ddof=1)), 4) if len(vals) > 1 else 0.0,
                p50=round(float(np.median(vals)), 4) if vals else 0.0,
            )
            for name, vals in groups.items()
        ]
        stats.sort(key=lambda s: -s.missions)

        d = ks = None
        verdict = "fewer than two cohorts with data"
        names = [s.cohort for s in stats[:2]]
        if len(names) == 2:
            from ..kernels.timeseries import cohens_d, ks_statistic

            a = np.asarray(groups[names[0]])
            b = np.asarray(groups[names[1]])
            d = round(cohens_d(a, b), 4)
            ks = round(ks_statistic(a, b), 4)
            mag = "negligible" if abs(d) < 0.2 else "small" if abs(d) < 0.5 else \
                  "moderate" if abs(d) < 0.8 else "large"
            verdict = (
                f"{metric} is {'higher' if d > 0 else 'lower'} in '{names[1]}' than "
                f"'{names[0]}' — {mag} effect (d={d}, KS={ks})"
            )
        return CohortComparison(
            metric=metric, split_by=split_by, cohorts=stats, cohens_d=d, ks=ks, verdict=verdict,
            provenance=Provenance(method=f"cohort_split({split_by})",
                                  sample_count=sum(s.missions for s in stats)),
        )

    @mcp.tool(name="compare.find_similar")
    def find_similar(
        mission_id: str,
        top_k: int = 5,
        signal_key: str | None = None,
    ) -> SimilarityResult:
        """"Has this happened before?" — the most valuable question in fleet debugging.

        Two passes: a cheap fingerprint (duration, topic set, rate profile, signal means,
        log-template overlap) shortlists candidates, then banded DTW ranks the shortlist
        on `signal_key` if one is given. Returns why each match was chosen.
        """
        cat = catalog()
        features = build_features(cat)
        idx = {f.mission_id: i for i, f in enumerate(features)}
        if mission_id not in idx:
            return SimilarityResult(
                target=mission_id, method="none",
                provenance=Provenance(warnings=["mission not indexed"]),
            )
        matrix = normalise_matrix(features)
        ranked = similarity(features[idx[mission_id]], features, matrix, idx[mission_id])
        shortlist = ranked[: max(top_k * 3, 10)]

        dtw: dict[str, float] = {}
        method = "feature_fingerprint"
        if signal_key:
            target_sig = load_signal(cat, mission_id, signal_key)
            if target_sig is not None:
                candidates = [
                    s for s in (load_signal(cat, mid, signal_key) for mid, _s, _d in shortlist)
                    if s is not None
                ]
                dtw = dict(dtw_rank(target_sig, candidates))
                method = f"fingerprint + banded DTW on {signal_key}"
                shortlist.sort(key=lambda x: dtw.get(x[0], float("inf")))

        paths = {
            r["mission_id"]: r["path"]
            for r in cat.query("SELECT mission_id, path FROM missions")
        }
        matches = []
        for mid, score, detail in shortlist[:top_k]:
            reasons = []
            if detail["topic_overlap"] > 0.9:
                reasons.append("same topic set")
            if detail["log_overlap"] > 0.5:
                reasons.append("similar log patterns")
            if detail["feature_distance"] < 0.5:
                reasons.append("similar shape and duration")
            if mid in dtw:
                reasons.append(f"DTW distance {dtw[mid]:.3f} on {signal_key}")
            matches.append(
                SimilarMission(
                    mission_id=mid,
                    path=paths.get(mid, ""),
                    score=round(score, 4),
                    dtw_distance=round(dtw[mid], 4) if mid in dtw else None,
                    detail=detail,
                    why=", ".join(reasons) or "closest on the combined fingerprint",
                )
            )
        return SimilarityResult(
            target=mission_id, matches=matches, method=method,
            provenance=Provenance(mission_id=mission_id, method=method,
                                  sample_count=len(features)),
        )

    @mcp.tool(name="compare.regression_scan")
    def regression_scan(metrics: list[str] | None = None, min_missions: int = 5) -> RegressionScan:
        """Sweep the corpus for metrics trending badly over time.

        Fits a slope per day across every indexed mission and flags the ones getting
        worse. Use it to notice a fleet-wide degradation nobody filed a ticket for.
        """
        from ..detectors.base import kendall_tau_p, theil_sen

        cat = catalog()
        wanted = metrics or ["health_score", "gap_count", "estimated_dropped",
                             "max_jitter_cv", "critical_events"]
        rows_out: list[RegressionRow] = []
        span_days = 0.0
        for metric in wanted:
            expr = METRICS.get(metric)
            if not expr:
                continue
            rows = cat.query(
                f"SELECT epoch(m.start_time) AS ts, {expr} AS v FROM missions m "
                f"WHERE m.start_time IS NOT NULL ORDER BY m.start_time"
            )
            if len(rows) < min_missions:
                continue
            xs = [float(r["ts"]) / 86400.0 for r in rows]
            ys = [float(r["v"] or 0.0) for r in rows]
            span_days = max(span_days, xs[-1] - xs[0])
            slope = theil_sen(xs, ys)
            _tau, p = kendall_tau_p(xs, ys)
            worsening = (slope < 0) if metric == "health_score" else (slope > 0)
            concern = (
                "worsening" if (worsening and p < 0.05)
                else "improving" if (not worsening and p < 0.05)
                else "no significant trend"
            )
            rows_out.append(
                RegressionRow(
                    metric=metric,
                    slope_per_day=round(slope, 6),
                    direction="down" if slope < 0 else "up",
                    n_missions=len(rows),
                    first_value=round(ys[0], 4),
                    last_value=round(ys[-1], 4),
                    concern=concern,
                )
            )
        rows_out.sort(key=lambda r: (r.concern != "worsening", -abs(r.slope_per_day)))
        return RegressionScan(
            rows=rows_out, window_days=round(span_days, 2),
            provenance=Provenance(method="theil_sen over mission start times",
                                  sample_count=len(rows_out)),
        )

    @mcp.tool(name="compare.rank_missions")
    def rank_missions(
        metric: str = "health_score", ascending: bool = True, limit: int = 20
    ) -> Ranking:
        """Rank every indexed mission by a metric, to find the worst offenders fast.

        Metrics: health_score, gap_count, estimated_dropped, max_jitter_cv, event_count,
        critical_events, duration_s, message_count, size_bytes.
        """
        cat = catalog()
        expr = METRICS.get(metric, METRICS["health_score"])
        order = "ASC" if ascending else "DESC"
        rows = cat.query(
            f"""SELECT m.mission_id, m.path, m.health_score, m.verdict, {expr} AS value
                FROM missions m ORDER BY value {order} LIMIT {int(limit)}"""
        )
        result = Ranking(
            metric=metric,
            ascending=ascending,
            missions=[
                RankedMission(
                    mission_id=r["mission_id"], path=r["path"],
                    value=round(float(r["value"] or 0), 4),
                    health_score=round(r["health_score"] or 0, 1),
                    verdict=r["verdict"] or "",
                )
                for r in rows
            ],
            provenance=Provenance(method=f"rank({metric})", sample_count=len(rows)),
        )

        def fewer(r: Ranking) -> Ranking:
            r.missions = r.missions[: max(5, len(r.missions) // 2)]
            return r

        return apply_budget(result, ladder=(fewer, fewer), narrowing="lower `limit`")
