"""Configuration: roots, budgets, thresholds, redaction.

Every magic number in the detector library lives here, documented and overridable.
Real robots differ; a hard-coded threshold is a bug report waiting to happen.

Environment overrides use the ``BAGLENS_`` prefix, e.g. ``BAGLENS_MAX_TOKENS=8000``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

Sensitivity = Literal["low", "normal", "high"]


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(f"BAGLENS_{name.upper()}")
    return float(raw) if raw else default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(f"BAGLENS_{name.upper()}")
    return int(raw) if raw else default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(f"BAGLENS_{name.upper()}")
    return raw not in ("0", "false", "no") if raw else default


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
    #: A topic is treated as having no cadence when its learned rate exceeds the rate it
    #: actually sustained across its own lifetime by this factor. Event-driven topics
    #: (`/event`, `/sensor_selection`, `/vehicle_command_ack`) publish in bursts, so the
    #: modal inter-arrival describes the spacing *inside* a burst — on real PX4 logs that
    #: produced learned rates 500–34000x the true one, and every rate-based detector then
    #: fired on a topic that was behaving perfectly.
    #:
    #: 5.0 is deliberately far above anything a real fault produces: an 8s dropout in a
    #: 90s recording moves this ratio to ~1.1, and a topic silent for half its life only
    #: reaches ~2.0, so genuine gaps are never mistaken for aperiodicity.
    aperiodic_ratio: float = 5.0


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
class DataAgeConfig:
    """F1 — end-to-end data age."""

    #: how many recent capture stamps to remember while inferring the propagation graph.
    #: Only messages still in flight can match, so this is generous; exceeding it is
    #: reported as truncation rather than silently losing links.
    stamp_table_size: int = 4096
    #: how long after a capture a downstream stage may still publish it. Evicting a stamp
    #: older than this loses nothing — no real pipeline stage was going to claim it — so
    #: only younger evictions are counted as truncation.
    link_horizon_s: float = 5.0
    #: a candidate upstream needs this many stamp matches before the edge is believed
    min_link_observations: int = 20
    #: and this fraction of the downstream topic's messages, so an occasional coincidence
    #: between unrelated topics does not become a pipeline stage
    min_link_fraction: float = 0.5
    #: at most this many candidate upstreams tracked per topic
    max_candidates_per_topic: int = 8

    #: trend on the age tail, in the same shape as D3: per-bucket P99, Theil-Sen slope
    bucket_s: float = 10.0
    n_buckets: int = 30
    min_buckets: int = 6
    #: growth in P99 age across the window that constitutes a finding, as a fraction of
    #: the earliest bucket's P99
    rel_growth_by_sensitivity: dict[str, float] = field(
        default_factory=lambda: {"low": 1.00, "normal": 0.50, "high": 0.25}
    )
    tau_p_max: float = 0.05
    #: A bucket needs this many age samples before its P99 is a statistic rather than a
    #: maximum. Measured, not guessed: without it, `nuway_stops` — the parked shuttle bus
    #: whose topics are event-driven — produced 16 false "data age is growing" findings,
    #: several from buckets holding four messages. At the 10 s default bucket this admits
    #: topics publishing above ~10 Hz, which is where latency matters anyway; slower
    #: topics are reported as unassessable for trend rather than judged.
    min_samples_per_bucket: int = 100
    #: ages below this are not worth trending; a 0.4 ms stage doubling is not a finding
    min_age_s: float = 0.002
    #: fraction of a topic's messages showing a stamp defect before it is reported
    restamp_fraction: float = 0.9
    #: An age larger than this is not an age — it is two different time bases being
    #: subtracted. `/bond` on a real shuttle-bus recording stamps from a steady clock that
    #: starts near zero, which reads as data 54 years old; reporting that next to a
    #: 48 ms lidar age would discredit both. Such topics are named unmeasurable instead.
    max_plausible_age_s: float = 60.0
    #: a stamp origin whose data is younger than this is stamping with its own publish
    #: time rather than a capture time. A real sensor never manages zero.
    restamp_max_age_s: float = 0.001


@dataclass(frozen=True)
class TransformsConfig:
    """F3 — transform integrity. `/tf` is many streams in one topic."""

    #: per parent→child state is small, but a tree is not. Bounded, with the truncation
    #: reported the way D2 and D7 report theirs.
    max_edges: int = 512
    #: two transforms for one stamp differing by more than this are two publishers
    #: fighting, not one publisher repeating itself
    disagreement_m: float = 0.05
    min_duplicate_observations: int = 10
    #: fraction of a transform's messages stamped ahead of their own publish time
    ahead_fraction: float = 0.5
    #: a dynamic transform absent from this fraction of buckets is intermittent
    bucket_s: float = 1.0
    min_buckets: int = 20
    min_presence: float = 0.95
    #: a gap counts once it exceeds this multiple of the transform's own period
    gap_factor: float = 3.0
    #: fraction of a consumer's messages newer than the whole tree before it is reported
    extrapolation_fraction: float = 0.05
    min_extrapolation_samples: int = 50
    #: A sensor stamp is *always* a few milliseconds newer than the last transform —
    #: transforms arrive at discrete instants, so between two of them the newest one is
    #: already stale. tf2 resolves that on the next cycle and nobody notices. Only a
    #: sensor further ahead than roughly one transform cycle is a genuine extrapolation
    #: risk; without this a healthy tree reports 6%, which is a race condition, not a bug.
    extrapolation_tolerance_s: float = 0.05
    #: extrapolation is only judged while transforms are actually flowing. Past this,
    #: the tree is simply not being published and the intermittency check owns the
    #: diagnosis — otherwise one TF outage is restated once per consumer topic.
    tf_live_window_s: float = 1.0


@dataclass(frozen=True)
class PreflightConfig:
    """F2 — the pre-flight readiness gate."""

    #: how long the gate watches before answering. Short on purpose: a gate that takes
    #: five minutes gets skipped, and a skipped gate is worse than none because it
    #: creates false confidence.
    window_s: float = 30.0
    #: observed rate may differ from the baseline by this fraction before it is a failure.
    #: A halved rate is -50% and must be caught; ordinary run-to-run variation must not be.
    rate_tolerance: float = 0.25
    #: a topic must deliver at least this many messages in the window to be judged at all
    min_messages: int = 20
    #: observed P95 data age may be this multiple of the baseline's before it fails
    age_tolerance: float = 2.0
    #: an absolute floor so a baseline captured at 3 ms does not fail on 7 ms of noise
    age_floor_ms: float = 20.0
    #: unchecked items do not fail the gate unless this is set. They are always listed:
    #: reporting them as passing is the one thing the gate must never do.
    strict: bool = False


@dataclass(frozen=True)
class CorrelationConfig:
    """D7 — cross-topic gap correlation."""

    window_s: float = 60.0
    #: another topic counts as co-silent if silent for >= this fraction of the gap
    overlap_frac: float = 0.5
    system_wide: float = 0.7
    isolated: float = 0.2
    #: bounded state: silent intervals retained for the whole recording, longest kept.
    #: The window itself is bounded by `window_s`; this bounds the history behind it.
    max_results: int = 1000
    #: A merged stall longer than this fraction of the stream is rejected as a modelling
    #: failure rather than reported. On a recording that is mostly event-driven topics,
    #: one interval can otherwise grow to span the entire file — measured at 1,489 s of a
    #: 1,492 s shuttle-bus rosbag. 0.5 is far above any real recorder stall in the PX4
    #: corpus (the longest is seconds) and far below the artefact, so it separates them
    #: without touching a labelled dropout.
    max_stall_fraction: float = _env_float("max_stall_fraction", 0.5)
    #: W15. Whether a topic with no usable rate model may open a silent interval, and
    #: whether it may count as co-silent inside someone else's.
    #:
    #: Both default to **False**, which reverses a decision this project held for two
    #: sessions. The register recorded that every restricted variant cost 22+ points of
    #: recall against PX4's own dropout labels, so D7 alone was left ignoring
    #: `unassessable` while D2, the per-topic scores and `find_gaps` all honoured it.
    #: That measurement was taken while the interval cap still ranked by duration — the
    #: bug that independently cost 35 of 152 labels — and it does not reproduce. Measured
    #: again on all 105 flights plus the injected ROS 2 labels (`scripts/w15_rules.py`,
    #: table in `evals/integrity/W15_RULES.md`):
    #:
    #:   | D7 rule                        | PX4 R/P     | injected R/P | nuway_stops     |
    #:   |--------------------------------|-------------|--------------|-----------------|
    #:   | unrestricted (was shipped)     | 0.993/0.942 | 0.824/1.000  | 627 s "stall"   |
    #:   | may not create                 | 0.993/0.942 | 0.824/1.000  | 7 s             |
    #:   | may not create or vote (ships) | 0.993/0.955 | 0.824/1.000  | 8 s             |
    #:
    #: Zero recall cost on either labelled corpus, 1.3 points of precision gained, and the
    #: phantom 627-second stall on a parked shuttle bus gone. Keep both knobs: this is the
    #: second time the answer has changed, and the next person needs to re-run it rather
    #: than trust this comment.
    aperiodic_may_create: bool = _env_bool("aperiodic_may_create", False)
    aperiodic_may_vote: bool = _env_bool("aperiodic_may_vote", False)


@dataclass(frozen=True)
class AssessabilityConfig:
    """When the report must refuse to give a verdict. See `detectors/assessability.py`.

    These are floors on how much of a recording was actually checked, not thresholds on
    how healthy it is. They are set where a reasonable engineer would stop trusting a
    summary rather than by fitting a corpus: half the traffic, a quarter of the topics,
    half the wall clock, and long enough for one cadence warmup to complete.
    """

    #: fraction of topics that must have a measurable publication rate
    min_topic_fraction: float = _env_float("min_topic_fraction", 0.25)
    #: share of all messages those topics must carry
    min_message_fraction: float = _env_float("min_message_fraction", 0.50)
    #: share of the recording during which some assessable topic was publishing
    min_coverage: float = _env_float("min_coverage", 0.50)
    #: below this, no topic can have completed cadence warmup (2x warmup_seconds)
    min_duration_s: float = _env_float("min_assessable_duration_s", 20.0)


@dataclass(frozen=True)
class ScoreConfig:
    """Health score weights. Published in the docs on purpose — an opaque score is ignored."""

    w_gap: float = 0.30
    w_drop: float = 0.35
    w_jitter: float = 0.20
    w_degradation: float = 0.15
    #: overall = (a*min(topic) + b*mean(topic) + c*file) * (1 - w_stall * stalled_fraction)
    a_min: float = 0.5
    b_mean: float = 0.3
    c_file: float = 0.2
    #: How hard a system-wide stall hits the overall score, as a multiplier on the
    #: fraction of the recording it consumed. Charged once here rather than per topic.
    #: Calibrated against 103 public PX4 flights: those lose a mean 4–8% of recording
    #: time to logger stalls and must still read `usable_with_caveats`, while a
    #: recording that spends half its length stalled must not.
    w_stall: float = 2.5
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
    data_age: DataAgeConfig = field(default_factory=DataAgeConfig)
    preflight: PreflightConfig = field(default_factory=PreflightConfig)
    transforms: TransformsConfig = field(default_factory=TransformsConfig)
    assessability: AssessabilityConfig = field(default_factory=AssessabilityConfig)
    score: ScoreConfig = field(default_factory=ScoreConfig)
    budget: BudgetConfig = field(default_factory=BudgetConfig)

    @property
    def current(self) -> Config:
        """Self. Mirrors ``_ConfigProxy.current`` so callers never need to know which
        of the two they are holding."""
        return self

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

    # The default windows are sized for accuracy on a workstation. BAGLENS_EDGE_PROFILE=1
    # shrinks them to fit the <2 KB/topic device budget; detection targets still hold,
    # with slightly noisier baselines (re-checked by running `evals.integrity.run` with
    # the variable set).
    #
    # The sizes are not guesses. Per topic the budget spends:
    #   cadence  8*hist_bins + 8*ring_size + 8*cv_window + 128  =  896 B
    #   gap      48*max_gaps + 64                               =  832 B
    #   degrad   8*n_buckets + 96                               =  192 B
    #   jitter   96 (its variance window is the cadence one)    =   96 B
    #                                                             ------
    #                                                             2016 B
    # Measured on a real 118-topic PX4 flight, the previous settings peaked at 6,160 B on
    # `/sensor_gyro_fft` — three times the budget, because `max_gaps` was left at its
    # workstation value of 1000 and one gappy topic can therefore hold 48 KB on its own.
    # A cap that only binds on a workstation is not a device budget.
    edge = os.environ.get("BAGLENS_EDGE_PROFILE") == "1"
    profile: dict[str, Any] = {}
    if edge:
        profile = {
            "cadence": CadenceConfig(hist_bins=32, ring_size=16),
            "jitter": JitterConfig(window=48),
            "degradation": DegradationConfig(n_buckets=12),
            "gap": GapConfig(max_gaps=16),
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


#: process-wide active configuration, replaced by server.main().
#: Typed as Config: the proxy forwards every attribute, and pretending otherwise
#: would push a union through every detector signature for no benefit.
if TYPE_CHECKING:
    CONFIG: Config
else:
    CONFIG = _ConfigProxy(load_config())


def set_config(cfg: Config) -> None:
    CONFIG._set(cfg)  # type: ignore[attr-defined]
