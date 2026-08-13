"""Cross-mission comparison kernels.

Alignment is the hard part: missions differ in length and phase, so comparing them by
absolute time is usually wrong. Three modes are supported and the mode is always
reported, because "these two missions differ" means nothing without saying how they
were lined up.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Literal

import numpy as np

from .timeseries import cohens_d, dtw_distance, ks_statistic, resample

AlignMode = Literal["absolute", "progress", "event"]


@dataclass
class MissionSignal:
    mission_id: str
    signal_key: str
    t: np.ndarray
    v: np.ndarray
    duration_s: float = 0.0

    def aligned(self, mode: AlignMode, event_t: float = 0.0) -> tuple[np.ndarray, np.ndarray]:
        if mode == "progress":
            span = self.duration_s or (float(self.t[-1]) if self.t.size else 1.0)
            return self.t / max(span, 1e-9), self.v
        if mode == "event":
            return self.t - event_t, self.v
        return self.t, self.v


def load_signal(catalog: Any, mission_id: str, signal_key: str) -> MissionSignal | None:
    """Read one cached signal from Parquet. Never reopens the bag."""
    import pyarrow.parquet as pq

    rows = catalog.query(
        "SELECT s.parquet_path, m.duration_s FROM signals s JOIN missions m "
        "USING (mission_id) WHERE s.mission_id = ? AND s.signal_key = ?",
        [mission_id, signal_key],
    )
    if not rows:
        return None
    table = pq.read_table(rows[0]["parquet_path"])
    return MissionSignal(
        mission_id=mission_id,
        signal_key=signal_key,
        t=table.column("t").to_numpy(zero_copy_only=False),
        v=table.column("v").to_numpy(zero_copy_only=False),
        duration_s=rows[0]["duration_s"] or 0.0,
    )


@dataclass
class SignalDiff:
    signal_key: str
    mean_a: float
    mean_b: float
    delta: float
    percent_change: float
    cohens_d: float
    ks: float
    magnitude: str

    @property
    def rank(self) -> float:
        return abs(self.cohens_d)


def _magnitude(d: float) -> str:
    a = abs(d)
    return "negligible" if a < 0.2 else "small" if a < 0.5 else "moderate" if a < 0.8 else "large"


def diff_signals(a: np.ndarray, b: np.ndarray, key: str) -> SignalDiff:
    ma = float(a.mean()) if a.size else 0.0
    mb = float(b.mean()) if b.size else 0.0
    d = cohens_d(a, b)
    return SignalDiff(
        signal_key=key,
        mean_a=round(ma, 6),
        mean_b=round(mb, 6),
        delta=round(mb - ma, 6),
        percent_change=round(100.0 * (mb - ma) / ma, 2) if ma else 0.0,
        cohens_d=round(d, 4),
        ks=round(ks_statistic(a, b), 4),
        magnitude=_magnitude(d),
    )


@dataclass
class MissionFeatures:
    """A cheap fingerprint for the first pass of similarity search.

    Comparing every pair with DTW over full signals would be quadratic in corpus size
    and minutes long. This vector is O(1) per mission and filters to a shortlist that
    DTW can then rank properly.
    """

    mission_id: str
    vector: np.ndarray
    topics: set[str] = field(default_factory=set)
    log_templates: dict[str, int] = field(default_factory=dict)


def build_features(catalog: Any, mission_ids: list[str] | None = None) -> list[MissionFeatures]:
    where = ""
    params: list[Any] = []
    if mission_ids:
        where = f" WHERE mission_id IN ({','.join('?' * len(mission_ids))})"
        params = list(mission_ids)

    missions = catalog.query(f"SELECT * FROM missions{where}", params)
    topics = catalog.query(f"SELECT * FROM topics{where}", params)
    signals = catalog.query(f"SELECT * FROM signals{where}", params)
    patterns = catalog.query(f"SELECT * FROM log_patterns{where}", params)
    events = catalog.query(
        f"SELECT mission_id, kind, COUNT(*) AS n FROM events{where} GROUP BY mission_id, kind",
        params,
    )

    by_topic: dict[str, set[str]] = {}
    hz: dict[str, list[float]] = {}
    for r in topics:
        by_topic.setdefault(r["mission_id"], set()).add(r["topic"])
        hz.setdefault(r["mission_id"], []).append(r["actual_hz"] or 0.0)
    sig: dict[str, dict[str, float]] = {}
    for r in signals:
        sig.setdefault(r["mission_id"], {})[r["signal_key"]] = r["mean"] or 0.0
    tmpl: dict[str, dict[str, int]] = {}
    for r in patterns:
        tmpl.setdefault(r["mission_id"], {})[r["template"]] = int(r["count"])
    ev: dict[str, dict[str, int]] = {}
    for r in events:
        ev.setdefault(r["mission_id"], {})[r["kind"]] = int(r["n"])

    all_signals = sorted({k for d in sig.values() for k in d})
    out: list[MissionFeatures] = []
    for m in missions:
        mid = m["mission_id"]
        base = [
            math.log1p(m.get("duration_s") or 0.0),
            math.log1p(m.get("message_count") or 0.0),
            (m.get("health_score") or 0.0) / 100.0,
            float(len(by_topic.get(mid, ()))),
            float(np.mean(hz.get(mid, [0.0]))),
            float(sum(ev.get(mid, {}).values())),
        ]
        base += [sig.get(mid, {}).get(k, 0.0) for k in all_signals]
        out.append(
            MissionFeatures(
                mission_id=mid,
                vector=np.asarray(base, dtype=float),
                topics=by_topic.get(mid, set()),
                log_templates=tmpl.get(mid, {}),
            )
        )
    return out


def normalise_matrix(features: list[MissionFeatures]) -> np.ndarray:
    if not features:
        return np.zeros((0, 0))
    m = np.vstack([f.vector for f in features])
    mu = m.mean(axis=0)
    sd = m.std(axis=0)
    sd[sd == 0] = 1.0
    return (m - mu) / sd


def jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    return len(a & b) / max(len(a | b), 1)


def similarity(
    target: MissionFeatures,
    others: list[MissionFeatures],
    matrix: np.ndarray,
    target_row: int,
) -> list[tuple[str, float, dict[str, float]]]:
    """Cheap first pass: feature distance + topic-set and log-template overlap."""
    scores: list[tuple[str, float, dict[str, float]]] = []
    tv = matrix[target_row]
    for i, other in enumerate(others):
        if other.mission_id == target.mission_id:
            continue
        d = float(np.linalg.norm(matrix[i] - tv)) / math.sqrt(max(matrix.shape[1], 1))
        topic_sim = jaccard(target.topics, other.topics)
        log_sim = jaccard(set(target.log_templates), set(other.log_templates))
        combined = 0.5 * math.exp(-d) + 0.3 * topic_sim + 0.2 * log_sim
        scores.append(
            (other.mission_id, combined,
             {"feature_distance": round(d, 4), "topic_overlap": round(topic_sim, 3),
              "log_overlap": round(log_sim, 3)})
        )
    scores.sort(key=lambda s: -s[1])
    return scores


def dtw_rank(
    target: MissionSignal, candidates: list[MissionSignal], bin_s: float = 1.0
) -> list[tuple[str, float]]:
    """Second pass: banded DTW on the shortlist only."""
    _t, ta, _g = resample(target.t, target.v, bin_s)
    ta = np.nan_to_num(ta, nan=float(np.nanmean(ta)) if ta.size else 0.0)
    out: list[tuple[str, float]] = []
    for c in candidates:
        _t2, cb, _g2 = resample(c.t, c.v, bin_s)
        cb = np.nan_to_num(cb, nan=float(np.nanmean(cb)) if cb.size else 0.0)
        out.append((c.mission_id, dtw_distance(ta, cb)))
    out.sort(key=lambda x: x[1])
    return out
