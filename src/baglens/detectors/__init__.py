"""The detector library — single-pass, bounded state, file or live subscription.

Deployed offline first; the same code is what will watch a robot live.
"""

from .auditor import ALL_DETECTORS, Auditor, TopicState
from .cadence import TopicCadence
from .clock import ClockDetector
from .correlation import CorrelationDetector
from .degradation import RateDegradationDetector
from .dropped import DroppedEstimator
from .gaps import Gap, GapDetector
from .jitter import JitterDetector
from .score import build_caveats, overall_score, topic_score, verdict_for

__all__ = [
    "ALL_DETECTORS",
    "Auditor",
    "TopicState",
    "TopicCadence",
    "GapDetector",
    "Gap",
    "RateDegradationDetector",
    "JitterDetector",
    "DroppedEstimator",
    "ClockDetector",
    "CorrelationDetector",
    "topic_score",
    "overall_score",
    "build_caveats",
    "verdict_for",
]
