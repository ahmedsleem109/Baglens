"""`timeseries.*` — numeric analysis that returns statistics, never payloads."""

from __future__ import annotations

from typing import Any, Literal

import numpy as np
from pydantic import BaseModel, Field

from ..budget import apply_budget, decimate
from ..config import CONFIG
from ..kernels.timeseries import (
    changepoints,
    cohens_d,
    cross_correlate,
    describe,
    iqr_outliers,
    ks_statistic,
    resample,
    rolling_mad_outliers,
    zscore_outliers,
)
from ..models import Budgeted
from ..provenance import Provenance, mission_id_for
from ..readers import open_bag
from .common import resolve


class SeriesResult(Budgeted):
    topic: str
    field_path: str
    bin_s: float
    t: list[float] = Field(default_factory=list)
    values: list[float | None] = Field(default_factory=list)
    stats: dict[str, float] = Field(default_factory=dict)
    #: windows where no sample existed — never interpolated across
    gaps: list[tuple[float, float]] = Field(default_factory=list)
    provenance: Provenance = Field(default_factory=Provenance)


class Anomaly(BaseModel):
    t: float
    value: float
    score: float


class AnomalyResult(Budgeted):
    topic: str
    field_path: str
    method: str
    anomalies: list[Anomaly] = Field(default_factory=list)
    total_found: int = 0
    provenance: Provenance = Field(default_factory=Provenance)


class Changepoint(BaseModel):
    t: float
    mean_before: float
    mean_after: float
    shift: float


class ChangepointResult(BaseModel):
    topic: str
    field_path: str
    changepoints: list[Changepoint] = Field(default_factory=list)
    provenance: Provenance = Field(default_factory=Provenance)


class CorrelationResult(BaseModel):
    topic_a: str
    field_a: str
    topic_b: str
    field_b: str
    best_lag_s: float
    correlation: float
    interpretation: str = ""
    provenance: Provenance = Field(default_factory=Provenance)


class WindowComparison(BaseModel):
    topic: str
    field_path: str
    window_a: tuple[float, float]
    window_b: tuple[float, float]
    stats_a: dict[str, float] = Field(default_factory=dict)
    stats_b: dict[str, float] = Field(default_factory=dict)
    cohens_d: float = 0.0
    ks_statistic: float = 0.0
    verdict: str = ""
    provenance: Provenance = Field(default_factory=Provenance)


def _extract(path: str, topic: str, field_path: str, start_s: float | None, end_s: float | None):
    p = resolve(path)
    reader = open_bag(p)
    meta = reader.metadata()
    t0 = meta.start_time_ns
    ts: list[float] = []
    vs: list[float] = []
    for t_ns, value in reader.numeric_field(topic, field_path):
        rel = (t_ns - t0) / 1e9
        if start_s is not None and rel < start_s:
            continue
        if end_s is not None and rel > end_s:
            break
        ts.append(rel)
        vs.append(value)
    reader.close()
    return p, meta, np.asarray(ts), np.asarray(vs)


def register(mcp: Any) -> None:
    @mcp.tool(name="timeseries.extract")
    def extract(
        path: str,
        topic: str,
        field_path: str,
        bin_s: float = 1.0,
        start_s: float | None = None,
        end_s: float | None = None,
    ) -> SeriesResult:
        """A numeric field resampled onto a uniform grid, plus its statistics.

        Returns downsampled values, never raw messages. Bins with no data are `null` and
        listed in `gaps` — values are never interpolated across a silence, because an
        interpolated point inside a sensor outage is a fabricated measurement.

        Raise `bin_s` if the response is truncated.
        """
        p, meta, ts, vs = _extract(path, topic, field_path, start_s, end_s)
        centres, binned, gaps = resample(ts, vs, bin_s)
        result = SeriesResult(
            topic=topic,
            field_path=field_path,
            bin_s=bin_s,
            t=[round(float(x), 4) for x in centres],
            values=[None if not np.isfinite(v) else round(float(v), 6) for v in binned],
            stats={k: round(v, 6) for k, v in describe(vs).items()},
            gaps=[(round(a, 3), round(b, 3)) for a, b in gaps],
            provenance=Provenance(
                path=str(p),
                mission_id=mission_id_for(p),
                topics=[topic],
                time_range=(start_s or 0.0, end_s or meta.duration_s),
                method=f"resample(bin_s={bin_s})",
                sample_count=int(ts.size),
                warnings=["values are binned means; gaps are not interpolated"] if gaps else [],
            ),
        )

        def coarser(r: SeriesResult) -> SeriesResult:
            keep = max(20, len(r.t) // 4)
            idx = decimate(list(range(len(r.t))), keep)
            r.t = [r.t[i] for i in idx]
            r.values = [r.values[i] for i in idx]
            r.bin_s = round(r.bin_s * (len(binned) / max(len(idx), 1)), 4)
            return r

        def stats_only(r: SeriesResult) -> SeriesResult:
            r.t = []
            r.values = []
            return r

        return apply_budget(
            result,
            ladder=(coarser, coarser, stats_only),
            narrowing=(
                f"{int(ts.size)} samples over "
                f"{meta.duration_s:.0f}s — raise bin_s or narrow start_s/end_s"
            ),
        )

    @mcp.tool(name="timeseries.detect_anomalies")
    def detect_anomalies(
        path: str,
        topic: str,
        field_path: str,
        method: Literal["mad", "zscore", "iqr"] = "mad",
        sensitivity: float = 5.0,
        start_s: float | None = None,
        end_s: float | None = None,
    ) -> AnomalyResult:
        """Outliers in a numeric field.

        "mad" (rolling median absolute deviation) is the default and the right choice for
        robot data: z-scores over a heavy-tailed distribution with one spike define the
        spike as normal. Lower `sensitivity` to find more.
        """
        p, meta, ts, vs = _extract(path, topic, field_path, start_s, end_s)
        if method == "zscore":
            hits = zscore_outliers(ts, vs, sensitivity)
        elif method == "iqr":
            hits = iqr_outliers(ts, vs, max(sensitivity / 3, 0.5))
        else:
            hits = rolling_mad_outliers(ts, vs, k=sensitivity)
        hits.sort(key=lambda h: -h[2])
        result = AnomalyResult(
            topic=topic,
            field_path=field_path,
            method=method,
            anomalies=[Anomaly(t=round(t, 4), value=round(v, 6), score=round(z, 3))
                       for t, v, z in hits[:200]],
            total_found=len(hits),
            provenance=Provenance(
                path=str(p), topics=[topic],
                time_range=(start_s or 0.0, end_s or meta.duration_s),
                method=f"{method}(k={sensitivity})", sample_count=int(ts.size),
            ),
        )

        def fewer(r: AnomalyResult) -> AnomalyResult:
            r.anomalies = r.anomalies[: max(10, len(r.anomalies) // 4)]
            return r

        return apply_budget(result, ladder=(fewer, fewer),
                            narrowing="raise sensitivity or narrow the time range")

    @mcp.tool(name="timeseries.detect_changepoints")
    def detect_changepoints(
        path: str, topic: str, field_path: str, max_changepoints: int = 5
    ) -> ChangepointResult:
        """Where did behaviour shift mid-mission?

        Binary segmentation on mean shift. Use when a metric looks different at the end
        of a run than at the start and you want the instant it changed, not the average.
        """
        p, meta, ts, vs = _extract(path, topic, field_path, None, None)
        cuts = changepoints(vs, max_cuts=max_changepoints)
        out = []
        bounds = [0, *cuts, vs.size]
        for i, cut in enumerate(cuts):
            before = vs[bounds[i]:cut]
            after = vs[cut:bounds[i + 2]]
            out.append(
                Changepoint(
                    t=round(float(ts[cut]), 4),
                    mean_before=round(float(before.mean()), 6) if before.size else 0.0,
                    mean_after=round(float(after.mean()), 6) if after.size else 0.0,
                    shift=round(float(after.mean() - before.mean()), 6)
                    if before.size and after.size
                    else 0.0,
                )
            )
        return ChangepointResult(
            topic=topic, field_path=field_path, changepoints=out,
            provenance=Provenance(
                path=str(p), topics=[topic], time_range=(0.0, meta.duration_s),
                method=f"binary_segmentation(max={max_changepoints})", sample_count=int(vs.size),
            ),
        )

    @mcp.tool(name="timeseries.correlate")
    def correlate(
        path: str,
        topic_a: str,
        field_a: str,
        topic_b: str,
        field_b: str,
        bin_s: float = 0.1,
        max_lag_s: float = 5.0,
    ) -> CorrelationResult:
        """Cross-correlation between two signals, including the lag.

        Answers "does CPU spike *before* the latency spike?" — a positive `best_lag_s`
        means B follows A. Correlation is not causation; the lag is the useful part.
        """
        p, meta, ta, va = _extract(path, topic_a, field_a, None, None)
        _p, _m, tb, vb = _extract(path, topic_b, field_b, None, None)
        _ca, ra, _ga = resample(ta, va, bin_s)
        _cb, rb, _gb = resample(tb, vb, bin_s)
        ra = np.nan_to_num(ra, nan=float(np.nanmean(ra)) if ra.size else 0.0)
        rb = np.nan_to_num(rb, nan=float(np.nanmean(rb)) if rb.size else 0.0)
        lag, corr = cross_correlate(ra, rb, bin_s, max_lag_s)
        if abs(corr) < 0.3:
            interp = "no meaningful relationship at any lag in range"
        elif abs(lag) < bin_s:
            interp = "the two move together with no measurable lead or lag"
        else:
            lead = topic_a if lag > 0 else topic_b
            interp = f"{lead} leads by {abs(lag):.2f}s (r={corr:.2f})"
        return CorrelationResult(
            topic_a=topic_a, field_a=field_a, topic_b=topic_b, field_b=field_b,
            best_lag_s=round(lag, 4), correlation=round(corr, 4), interpretation=interp,
            provenance=Provenance(
                path=str(p), topics=[topic_a, topic_b], time_range=(0.0, meta.duration_s),
                method=f"cross_correlation(bin_s={bin_s},max_lag={max_lag_s})",
                sample_count=int(ta.size + tb.size),
            ),
        )

    @mcp.tool(name="timeseries.compare_windows")
    def compare_windows(
        path: str,
        topic: str,
        field_path: str,
        a_start_s: float,
        a_end_s: float,
        b_start_s: float,
        b_end_s: float,
    ) -> WindowComparison:
        """Same signal, two windows of the same mission. Regression-within-run.

        Returns effect sizes (Cohen's d, KS) rather than raw values, so the answer is
        "how much did it change" not "here are 40,000 numbers".
        """
        p, meta, ta, va = _extract(path, topic, field_path, a_start_s, a_end_s)
        _p, _m, _tb, vb = _extract(path, topic, field_path, b_start_s, b_end_s)
        d = cohens_d(va, vb)
        ks = ks_statistic(va, vb)
        magnitude = (
            "negligible" if abs(d) < 0.2 else
            "small" if abs(d) < 0.5 else
            "moderate" if abs(d) < 0.8 else "large"
        )
        return WindowComparison(
            topic=topic, field_path=field_path,
            window_a=(a_start_s, a_end_s), window_b=(b_start_s, b_end_s),
            stats_a={k: round(v, 6) for k, v in describe(va).items()},
            stats_b={k: round(v, 6) for k, v in describe(vb).items()},
            cohens_d=round(d, 4), ks_statistic=round(ks, 4),
            verdict=f"{magnitude} shift ({'higher' if d > 0 else 'lower'} in window B)",
            provenance=Provenance(
                path=str(p), topics=[topic], time_range=(a_start_s, b_end_s),
                method="cohens_d + ks_2samp", sample_count=int(va.size + vb.size),
            ),
        )

    _ = CONFIG
