"""Configuration: roots, budgets, thresholds, redaction.

Every magic number in the detector library lives here, documented and overridable.
Real robots differ; a hard-coded threshold is a bug report waiting to happen.

Environment overrides use the ``BAGLENS_`` prefix, e.g. ``BAGLENS_MAX_TOKENS=8000``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

Sensitivity = Literal["low", "normal", "high"]


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(f"BAGLENS_{name.upper()}")
    return float(raw) if raw else default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(f"BAGLENS_{name.upper()}")
    return int(raw) if raw else default


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(f"BAGLENS_{name.upper()}")
    return Path(raw).expanduser() if raw else default


@dataclass(frozen=True)
class CadenceConfig:
    """D1 — cadence baseline."""

    warmup_messages: int = 50
    warmup_seconds: float = 10.0
    hist_bins: int = 64
    hist_min_s: float = 1e-3
    hist_max_s: float = 10.0
    #: median is taken over inter-arrivals within +/- this fraction of the modal bin
    mode_refine_frac: float = 0.25
    ring_size: int = 50


@dataclass(frozen=True)
class GapConfig:
    """D2 — gap detection."""

    #: gap when dt > k * expected_period; scaled by sensitivity
    k_by_sensitivity: dict[str, float] = field(
        default_factory=lambda: {"low": 7.0, "normal": 5.0, "high": 3.5}
    )
    #: absolute floor so 100 Hz topics do not fire on a 50 ms hiccup
    floor_s: float = 0.25
    #: gaps closer together than this many periods are merged
    merge_periods: float = 2.0
    #: bounded state: keep at most this many gaps, largest-first, and report truncation
    max_gaps: int = 1000
    #: severity ladder, in multiples of the expected period
    sev_low: float = 5.0
    sev_medium: float = 20.0
    sev_high: float = 100.0
    sev_critical: float = 1000.0
    #: any silence longer than this is CRITICAL regardless of period
    critical_absolute_s: float = 30.0


@dataclass(frozen=True)
class DegradationConfig:
    """D3 — rate degradation (streaming trend)."""

    ewma_alpha: float = 0.02  # ~50-sample memory
    bucket_s: float = 10.0
    n_buckets: int = 30  # Theil-Sen over the last 30 buckets
    min_buckets: int = 8  # do not fire before this much history
    rel_slope_by_sensitivity: dict[str, float] = field(
        default_factory=lambda: {"low": 0.25, "normal": 0.15, "high": 0.08}
    )
    tau_p_max: float = 0.05


@dataclass(frozen=True)
class JitterConfig:
    """D4 — jitter expansion."""

    window: int = 200
    cv_multiplier_by_sensitivity: dict[str, float] = field(
        default_factory=lambda: {"low": 3.0, "normal": 2.0, "high": 1.5}
    )
    cv_floor_by_sensitivity: dict[str, float] = field(
        default_factory=lambda: {"low": 0.50, "normal": 0.35, "high": 0.22}
    )
    sustain_s: float = 5.0


@dataclass(frozen=True)
class ClockConfig:
    """D6 — clock sanity."""

    lag_ewma_alpha: float = 0.01
    #: recorder lag growth over the run that constitutes a finding
    lag_growth_s: float = 0.100
    #: absolute recorder lag that constitutes a finding
    lag_absolute_s: float = 0.500
    #: |d(log_time) - d(publish_time)| in one message that counts as a clock step
    step_s: float = 0.500
    #: downsampled lag curve length returned by health.clock_report
    lag_curve_points: int = 100


@dataclass(frozen=True)
class CorrelationConfig:
    """D7 — cross-topic gap correlation."""

    window_s: float = 60.0
    #: another topic counts as co-silent if silent for >= this fraction of the gap
    overlap_frac: float = 0.5
    system_wide: float = 0.7
    isolated: float = 0.2


@dataclass(frozen=True)
class ScoreConfig:
    """Health score weights. Published in the docs on purpose — an opaque score is ignored."""

    w_gap: float = 0.30
    w_drop: float = 0.35
    w_jitter: float = 0.20
    w_degradation: float = 0.15
    #: overall = a*min(topic) + b*mean(topic) + c*file
    a_min: float = 0.5
    b_mean: float = 0.3
    c_file: float = 0.2
    trustworthy: float = 85.0
    usable: float = 60.0


@dataclass(frozen=True)
class BudgetConfig:
    """G2 — response budgeting."""

    max_tokens: int = 4000
    session_soft_cap: int = 25000
    #: chars per token, for the network-free estimator
    chars_per_token: float = 3.6
    max_findings: int = 15


@dataclass(frozen=True)
class Config:
    roots: tuple[Path, ...] = ()
    cache_dir: Path = Path.home() / ".baglens"
    sensitivity: Sensitivity = "normal"
    allow_frames: bool = True
    #: topics/field paths masked before results leave a tool
    redact_topics: tuple[str, ...] = ()
    redact_fields: tuple[str, ...] = ()

    cadence: CadenceConfig = field(default_factory=CadenceConfig)
    gap: GapConfig = field(default_factory=GapConfig)
    degradation: DegradationConfig = field(default_factory=DegradationConfig)
    jitter: JitterConfig = field(default_factory=JitterConfig)
    clock: ClockConfig = field(default_factory=ClockConfig)
    correlation: CorrelationConfig = field(default_factory=CorrelationConfig)
    score: ScoreConfig = field(default_factory=ScoreConfig)
    budget: BudgetConfig = field(default_factory=BudgetConfig)

    @property
    def catalog_path(self) -> Path:
        return self.cache_dir / "catalog.duckdb"

    @property
    def signal_dir(self) -> Path:
        return self.cache_dir / "signals"

    @property
    def artifact_dir(self) -> Path:
        return self.cache_dir / "artifacts"

    def resolve(self, path: str | Path) -> Path:
        """Resolve a user-supplied path, rejecting escapes from the configured roots."""
        p = Path(path).expanduser().resolve()
        if not self.roots:
            return p
        for root in self.roots:
            r = root.expanduser().resolve()
            if p == r or r in p.parents:
                return p
        raise PermissionError(f"path {p} is outside the configured baglens roots")


def load_config(
    roots: list[str] | None = None,
    sensitivity: Sensitivity | None = None,
    allow_frames: bool = True,
) -> Config:
    env_roots = os.environ.get("BAGLENS_ROOTS", "")
    root_list = list(roots or [])
    if env_roots:
        root_list += [r for r in env_roots.split(os.pathsep) if r]
    sens = sensitivity or os.environ.get("BAGLENS_SENSITIVITY", "normal")
    if sens not in ("low", "normal", "high"):
        sens = "normal"
    cache = _env_path("cache_dir", Path.home() / ".baglens")
    cache.mkdir(parents=True, exist_ok=True)

    # The default windows are sized for accuracy on a workstation (~3.1 KB of state per
    # topic). BAGLENS_EDGE_PROFILE=1 shrinks them to fit the <2 KB/topic device budget;
    # detection targets still hold, with slightly noisier baselines.
    edge = os.environ.get("BAGLENS_EDGE_PROFILE") == "1"
    profile: dict[str, Any] = {}
    if edge:
        profile = {
            "cadence": CadenceConfig(hist_bins=48, ring_size=32),
            "jitter": JitterConfig(window=96),
            "degradation": DegradationConfig(n_buckets=24),
        }

    return Config(
        **profile,
        roots=tuple(Path(r).expanduser() for r in root_list),
        cache_dir=cache,
        sensitivity=sens,  # type: ignore[arg-type]
        allow_frames=allow_frames and os.environ.get("BAGLENS_NO_FRAMES") != "1",
        redact_topics=tuple(t for t in os.environ.get("BAGLENS_REDACT_TOPICS", "").split(",") if t),
        redact_fields=tuple(f for f in os.environ.get("BAGLENS_REDACT_FIELDS", "").split(",") if f),
        budget=BudgetConfig(
            max_tokens=_env_int("max_tokens", 4000),
            session_soft_cap=_env_int("session_soft_cap", 25000),
            chars_per_token=_env_float("chars_per_token", 3.6),
        ),
    )


class _ConfigProxy:
    """Forwards attribute reads to the active configuration.

    Modules do ``from .config import CONFIG`` at import time. If CONFIG were a plain
    module global, rebinding it in ``set_config`` would leave every importer holding the
    old object — which is exactly how a `--root` confinement silently stops applying.
    The proxy keeps one indirection so there is only ever one live config.
    """

    __slots__ = ("_current",)

    def __init__(self, cfg: Config) -> None:
        object.__setattr__(self, "_current", cfg)

    @property
    def current(self) -> Config:
        return object.__getattribute__(self, "_current")

    def _set(self, cfg: Config) -> None:
        object.__setattr__(self, "_current", cfg)

    def __getattr__(self, name: str) -> Any:
        return getattr(object.__getattribute__(self, "_current"), name)

    def __repr__(self) -> str:
        return f"<active {self.current!r}>"


#: process-wide active configuration, replaced by server.main()
CONFIG = _ConfigProxy(load_config())


def set_config(cfg: Config) -> None:
    CONFIG._set(cfg)
