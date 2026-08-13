"""D1 — cadence baseline.

Establish the expected rate per topic without configuration. This is exactly the
warmup a device performs on install: learn the robot's normal in the first minutes.

State per topic: 64 histogram ints + a 50-element ring + a handful of scalars. ~1 KB.
"""

from __future__ import annotations

from collections import deque
from statistics import median

from ..config import CONFIG, CadenceConfig
from .base import LogHistogram, RollingWelford


class TopicCadence:
    """Warmup-derived expected period, plus the running counters everyone else needs."""

    __slots__ = (
        "topic",
        "cfg",
        "hist",
        "ring",
        "count",
        "first_t",
        "last_t",
        "expected_period",
        "hz_source",
        "ready",
        "_warm_t0",
        "cv_window",
        "cv_baseline",
        "bytes_total",
        "declared_period",
        "qos_mismatch",
        "period_cv",
    )

    def __init__(self, topic: str, cfg: CadenceConfig | None = None,
                 declared_period_s: float | None = None,
                 cv_window_size: int | None = None) -> None:
        self.topic = topic
        self.cfg = cfg or CONFIG.cadence
        self.hist = LogHistogram(self.cfg.hist_bins, self.cfg.hist_min_s, self.cfg.hist_max_s)
        self.ring: deque[float] = deque(maxlen=self.cfg.ring_size)
        self.count = 0
        self.bytes_total = 0
        self.first_t: float | None = None
        self.last_t: float | None = None
        self._warm_t0: float | None = None
        # sized from the jitter config so D4 can share this window instead of
        # keeping a second copy of the same statistic
        self.cv_window = RollingWelford(cv_window_size or CONFIG.jitter.window)
        self.cv_baseline: float | None = None
        #: jitter of the *on-cadence* arrivals only — drops and gaps excluded. This is
        #: the uncertainty in the period estimate, and nothing else should be used as one.
        self.period_cv = 0.02

        # A declared QoS deadline is a contract, not a measurement. Prefer it — but
        # only once warmup shows the topic actually honours it. A declared rate that
        # disagrees with the observed one is itself a finding (a QoS mismatch), and
        # trusting it blindly would silently mis-scale every downstream threshold.
        self.declared_period = declared_period_s if (declared_period_s or 0) > 0 else None
        self.qos_mismatch = False
        self.expected_period: float | None = None
        self.hz_source = "qos" if self.declared_period else "modal"
        self.ready = False

    # -- streaming ---------------------------------------------------------

    def push(self, t: float, size: int = 0) -> float | None:
        """Feed one arrival. Returns the inter-arrival, or None for the first message."""
        self.count += 1
        self.bytes_total += size
        if self.first_t is None:
            self.first_t = t
            self._warm_t0 = t
            self.last_t = t
            return None
        dt = t - self.last_t  # type: ignore[operator]
        self.last_t = t
        if dt <= 0:
            return dt
        self.hist.push(dt)
        self.ring.append(dt)
        # A gap is not jitter. Feeding a 5-second silence into the variance window
        # would make every dropout look like a timing problem as well, and would
        # inflate the jitter_cv reported for an otherwise metronomic topic.
        period = self.expected_period or (self.hist.mode() if self.hist.total >= 5 else None)
        if period is None or dt <= 5.0 * period:
            self.cv_window.push(dt)

        if not self.ready:
            warm_msgs = self.count >= self.cfg.warmup_messages
            # `or t` would be wrong here: the first topic in a recording starts at
            # exactly t=0.0, which is falsy, and warmup would never complete for it
            warm_t0 = self._warm_t0 if self._warm_t0 is not None else t
            warm_time = (t - warm_t0) >= self.cfg.warmup_seconds
            if warm_msgs and warm_time:
                self._finish_warmup()
        return dt

    def _finish_warmup(self) -> None:
        mode = self.hist.mode()
        if mode <= 0:
            return
        # The modal *bin* is only accurate to its own width (~15% at 64 log bins), so
        # taking the median of a narrow band around the bin centre inherits that bias —
        # and a 4% bias in the period turns straight into a 4% phantom drop rate.
        # Use the bin only to reject gaps, then let the ring decide the value.
        wide = [d for d in self.ring if 0.5 * mode < d < 1.5 * mode]
        observed = median(wide) if wide else mode
        narrow = [d for d in wide if abs(d - observed) < self.cfg.mode_refine_frac * observed]
        if narrow:
            observed = median(narrow)
        if len(narrow) > 2:
            m = sum(narrow) / len(narrow)
            var = sum((d - m) ** 2 for d in narrow) / (len(narrow) - 1)
            self.period_cv = (var**0.5) / m if m > 0 else 0.02

        if self.declared_period and (observed / 3.0) <= self.declared_period <= (observed * 3.0):
            self.expected_period = self.declared_period
            self.hz_source = "qos"
        else:
            self.expected_period = observed
            self.hz_source = "modal"
            self.qos_mismatch = self.declared_period is not None
        self.ready = True
        self.cv_baseline = self.cv_window.cv if self.cv_window.n >= 10 else 0.0

    # -- reads -------------------------------------------------------------

    @property
    def provisional_period(self) -> float | None:
        """Best estimate available right now, warm or not — gaps cannot wait for warmup."""
        if self.expected_period:
            return self.expected_period
        if self.declared_period:
            return self.declared_period
        if self.hist.total >= 5:
            return self.hist.mode()
        if self.ring:
            return median(self.ring)
        return None

    @property
    def expected_hz(self) -> float | None:
        p = self.expected_period or self.provisional_period
        return 1.0 / p if p else None

    def observed_hz(self, t_end: float, silent_s: float = 0.0) -> float:
        """Messages per second of *active* time — silence is excluded, not averaged in."""
        if self.first_t is None:
            return 0.0
        active = max(t_end - self.first_t - silent_s, 1e-9)
        return self.count / active

    @property
    def jitter_cv(self) -> float:
        return self.cv_window.cv

    def state_bytes(self) -> int:
        return 8 * self.cfg.hist_bins + 8 * self.cfg.ring_size + 8 * self.cv_window.window + 128
