"""Numeric analysis kernels.

Resampling never interpolates across a gap silently — gaps are marked, because an
interpolated value inside a 12-second sensor outage is a fabricated measurement, and
an agent cannot tell the difference unless we say so.

Outliers use rolling MAD rather than z-scores: robot data has heavy tails, and a
z-score over a distribution with one spike defines the spike as normal.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np


@dataclass
class Series:
    t: list[float] = field(default_factory=list)
    v: list[float] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.t)

    def arrays(self) -> tuple[np.ndarray, np.ndarray]:
        return np.asarray(self.t, dtype=float), np.asarray(self.v, dtype=float)


def describe(values: np.ndarray) -> dict[str, float]:
    if values.size == 0:
        return {}
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return {"count": 0.0}
    q = np.percentile(finite, [1, 5, 25, 50, 75, 95, 99])
    return {
        "count": float(finite.size),
        "min": float(finite.min()),
        "max": float(finite.max()),
        "mean": float(finite.mean()),
        "std": float(finite.std(ddof=1)) if finite.size > 1 else 0.0,
        "p1": float(q[0]),
        "p5": float(q[1]),
        "p25": float(q[2]),
        "p50": float(q[3]),
        "p75": float(q[4]),
        "p95": float(q[5]),
        "p99": float(q[6]),
    }


def resample(
    t: np.ndarray, v: np.ndarray, bin_s: float, gap_factor: float = 4.0
) -> tuple[np.ndarray, np.ndarray, list[tuple[float, float]]]:
    """Bin to a uniform grid. Bins with no samples become NaN and are reported as gaps."""
    if t.size == 0:
        return np.array([]), np.array([]), []
    t0, t1 = float(t[0]), float(t[-1])
    n = max(1, int(math.ceil((t1 - t0) / bin_s)))
    edges = t0 + np.arange(n + 1) * bin_s
    idx = np.clip(np.searchsorted(edges, t, side="right") - 1, 0, n - 1)

    sums = np.zeros(n)
    counts = np.zeros(n)
    np.add.at(sums, idx, v)
    np.add.at(counts, idx, 1)
    out = np.where(counts > 0, sums / np.maximum(counts, 1), np.nan)
    centres = edges[:-1] + bin_s / 2

    gaps: list[tuple[float, float]] = []
    empty = counts == 0
    start = None
    for i, e in enumerate(empty):
        if e and start is None:
            start = i
        elif not e and start is not None:
            gaps.append((float(edges[start]), float(edges[i])))
            start = None
    if start is not None:
        gaps.append((float(edges[start]), float(edges[n])))
    _ = gap_factor
    return centres, out, gaps


def rolling_mad_outliers(
    t: np.ndarray, v: np.ndarray, window: int = 101, k: float = 5.0
) -> list[tuple[float, float, float]]:
    """(time, value, robust_z) for points far from their local median."""
    if v.size < 5:
        return []
    window = min(window if window % 2 else window + 1, v.size if v.size % 2 else v.size - 1)
    window = max(window, 5)
    half = window // 2
    padded = np.pad(v, half, mode="edge")
    strides = np.lib.stride_tricks.sliding_window_view(padded, window)
    med = np.median(strides, axis=1)
    mad = np.median(np.abs(strides - med[:, None]), axis=1)
    scale = 1.4826 * mad
    scale = np.where(scale > 0, scale, np.nan)
    z = np.abs(v - med) / scale
    hits = np.where(np.isfinite(z) & (z > k))[0]
    return [(float(t[i]), float(v[i]), float(z[i])) for i in hits]


def zscore_outliers(t: np.ndarray, v: np.ndarray, k: float = 4.0) -> list[tuple[float, float, float]]:
    if v.size < 3:
        return []
    mu, sd = float(v.mean()), float(v.std(ddof=1))
    if sd <= 0:
        return []
    z = np.abs(v - mu) / sd
    return [(float(t[i]), float(v[i]), float(z[i])) for i in np.where(z > k)[0]]


def iqr_outliers(t: np.ndarray, v: np.ndarray, k: float = 1.5) -> list[tuple[float, float, float]]:
    if v.size < 4:
        return []
    q1, q3 = np.percentile(v, [25, 75])
    iqr = q3 - q1
    if iqr <= 0:
        return []
    lo, hi = q1 - k * iqr, q3 + k * iqr
    hits = np.where((v < lo) | (v > hi))[0]
    return [(float(t[i]), float(v[i]), float(abs(v[i] - np.median(v)) / iqr)) for i in hits]


def changepoints(v: np.ndarray, max_cuts: int = 5, min_size: int = 20) -> list[int]:
    """Binary segmentation on mean shift. Returns sorted sample indices.

    Deliberately simple: a full PELT implementation buys precision the agent cannot
    use, while this reliably answers "where did behaviour shift?".
    """
    cuts: list[int] = []

    def cost(seg: np.ndarray) -> float:
        return float(seg.size * seg.var()) if seg.size else 0.0

    def split(lo: int, hi: int) -> tuple[float, int] | None:
        if hi - lo < 2 * min_size:
            return None
        base = cost(v[lo:hi])
        best, best_i = 0.0, -1
        for i in range(lo + min_size, hi - min_size):
            gain = base - cost(v[lo:i]) - cost(v[i:hi])
            if gain > best:
                best, best_i = gain, i
        return (best, best_i) if best_i > 0 else None

    segments = [(0, v.size)]
    for _ in range(max_cuts):
        candidates = [(s := split(lo, hi), lo, hi) for lo, hi in segments]
        scored = [(c[0], c[1]) for c, _lo, _hi in candidates if c]
        if not scored:
            break
        gain, idx = max(scored)
        if gain <= 0:
            break
        cuts.append(idx)
        segments = []
        bounds = [0, *sorted(cuts), v.size]
        segments = list(zip(bounds[:-1], bounds[1:], strict=False))
        _ = s
    return sorted(cuts)


def cross_correlate(
    a: np.ndarray, b: np.ndarray, dt: float, max_lag_s: float = 5.0
) -> tuple[float, float]:
    """Best (lag_seconds, correlation) of ``b`` against ``a``. Positive lag: b follows a."""
    n = min(a.size, b.size)
    if n < 8:
        return 0.0, 0.0
    a = a[:n] - a[:n].mean()
    b = b[:n] - b[:n].mean()
    denom = math.sqrt(float((a * a).sum()) * float((b * b).sum()))
    if denom <= 0:
        return 0.0, 0.0
    max_lag = min(int(max_lag_s / dt), n - 2) if dt > 0 else 0
    best_lag, best_corr = 0, 0.0
    for lag in range(-max_lag, max_lag + 1):
        if lag >= 0:
            corr = float((a[: n - lag] * b[lag:]).sum()) / denom
        else:
            corr = float((a[-lag:] * b[: n + lag]).sum()) / denom
        if abs(corr) > abs(best_corr):
            best_lag, best_corr = lag, corr
    return best_lag * dt, best_corr


def cohens_d(a: np.ndarray, b: np.ndarray) -> float:
    if a.size < 2 or b.size < 2:
        return 0.0
    pooled = math.sqrt(((a.size - 1) * a.var(ddof=1) + (b.size - 1) * b.var(ddof=1))
                       / (a.size + b.size - 2))
    return float((b.mean() - a.mean()) / pooled) if pooled > 0 else 0.0


def ks_statistic(a: np.ndarray, b: np.ndarray) -> float:
    if a.size == 0 or b.size == 0:
        return 0.0
    xs = np.sort(np.concatenate([a, b]))
    ca = np.searchsorted(np.sort(a), xs, side="right") / a.size
    cb = np.searchsorted(np.sort(b), xs, side="right") / b.size
    return float(np.max(np.abs(ca - cb)))


def dtw_distance(a: np.ndarray, b: np.ndarray, band: int = 20) -> float:
    """Sakoe-Chiba banded DTW on z-normalised series. O(n*band), not O(n^2)."""
    if a.size == 0 or b.size == 0:
        return float("inf")
    a = (a - a.mean()) / (a.std() or 1.0)
    b = (b - b.mean()) / (b.std() or 1.0)
    n, m = a.size, b.size
    inf = float("inf")
    prev = np.full(m + 1, inf)
    prev[0] = 0.0
    for i in range(1, n + 1):
        cur = np.full(m + 1, inf)
        lo = max(1, i - band)
        hi = min(m, i + band)
        for j in range(lo, hi + 1):
            cost = (a[i - 1] - b[j - 1]) ** 2
            cur[j] = cost + min(prev[j], cur[j - 1], prev[j - 1])
        prev = cur
    return float(math.sqrt(prev[m] / max(n, m))) if prev[m] < inf else inf
