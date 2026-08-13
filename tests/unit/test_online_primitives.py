"""The online primitives, against known-answer cases and property tests.

These are the pieces whose correctness is not obvious by inspection, which is exactly
why they are tested directly rather than only through the detectors.
"""

from __future__ import annotations

import math
import statistics

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from baglens.detectors.base import (
    Ewma,
    LogHistogram,
    RollingWelford,
    Welford,
    kendall_tau_p,
    theil_sen,
)
from baglens.detectors.clock import DecimatingCurve

FLOATS = st.floats(min_value=-1e6, max_value=1e6, allow_nan=False, allow_infinity=False)


@given(st.lists(FLOATS, min_size=2, max_size=200))
@settings(max_examples=50, deadline=None)
def test_welford_matches_statistics(xs: list[float]) -> None:
    w = Welford()
    for x in xs:
        w.push(x)
    assert w.mean == pytest.approx(statistics.fmean(xs), rel=1e-6, abs=1e-6)
    assert w.std == pytest.approx(statistics.stdev(xs), rel=1e-4, abs=1e-4)


@given(st.lists(st.floats(min_value=0.001, max_value=1000), min_size=5, max_size=300))
@settings(max_examples=50, deadline=None)
def test_rolling_welford_matches_window(xs: list[float]) -> None:
    window = 20
    rw = RollingWelford(window)
    for x in xs:
        rw.push(x)
    tail = xs[-window:]
    assert rw.n == len(tail)
    assert rw.mean == pytest.approx(statistics.fmean(tail), rel=1e-6, abs=1e-6)
    if len(tail) > 1:
        assert rw.std == pytest.approx(statistics.stdev(tail), rel=1e-3, abs=1e-6)


def test_rolling_welford_state_is_bounded() -> None:
    rw = RollingWelford(50)
    for i in range(10_000):
        rw.push(float(i % 7))
    assert len(rw.buf) == 50


def test_log_histogram_finds_the_mode_despite_gaps() -> None:
    """The whole point: the mean is destroyed by the gaps we are hunting for."""
    hist = LogHistogram(64, 1e-3, 10.0)
    period = 0.01
    for _ in range(500):
        hist.push(period)
    for _ in range(20):
        hist.push(5.0)  # gaps
    assert hist.mode() == pytest.approx(period, rel=0.2)
    naive_mean = (500 * period + 20 * 5.0) / 520
    assert naive_mean > 5 * period  # the estimator we deliberately did not use


def test_ewma_converges() -> None:
    e = Ewma(0.1)
    for _ in range(500):
        e.push(3.0)
    assert e.value == pytest.approx(3.0, rel=1e-6)


def test_theil_sen_ignores_an_outlier() -> None:
    xs = [float(i) for i in range(30)]
    ys = [2.0 * x + 1.0 for x in xs]
    ys[15] = 10_000.0
    assert theil_sen(xs, ys) == pytest.approx(2.0, rel=1e-6)


def test_theil_sen_flat_series() -> None:
    xs = [float(i) for i in range(10)]
    assert theil_sen(xs, [5.0] * 10) == pytest.approx(0.0)


def test_kendall_tau_detects_monotone_trend() -> None:
    xs = [float(i) for i in range(30)]
    tau, p = kendall_tau_p(xs, [-0.5 * x for x in xs])
    assert tau == pytest.approx(-1.0)
    assert p < 0.001


def test_kendall_tau_on_noise_is_not_significant() -> None:
    import random

    rng = random.Random(0)
    xs = [float(i) for i in range(30)]
    ys = [rng.gauss(0, 1) for _ in xs]
    _tau, p = kendall_tau_p(xs, ys)
    assert p > 0.05


def test_decimating_curve_is_bounded_and_spans_the_run() -> None:
    curve = DecimatingCurve(cap=100)
    for i in range(100_000):
        curve.push(i * 0.01, math.sin(i / 1000))
    assert len(curve.ts) <= 100
    assert curve.ts[0] < 10.0
    assert curve.ts[-1] > 900.0  # still covers the end of the run
    ts, vs = curve.downsample(50)
    assert len(ts) == len(vs) == 50
