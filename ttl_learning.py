"""ttl_learning.py – Per-tool cache TTL learning with weighted strategy blending."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MIN_SAMPLE_GATE: int = 5            # events before any adjustment is considered
DEFAULT_ALPHA: float = 0.3          # EWMA smoothing factor
DAMPING_FACTOR: float = 0.85        # multiplicative damping to suppress oscillation

# Strategy base weights (40 / 40 / 20)
_W_HIT_RATE: float = 0.40
_W_COST: float = 0.40
_W_RECENCY: float = 0.20

# TTL adjustment bounds (multiplicative)
_MIN_ADJUSTMENT: float = 0.5        # never cut TTL below 50 %
_MAX_ADJUSTMENT: float = 2.0        # never grow TTL beyond 200 %

# Neutral fallbacks
_NEUTRAL_HIT_RATE: float = 0.5


class LearningMode(Enum):
    """Controls how aggressively TTL can be adjusted."""
    CONSERVATIVE = "conservative"
    MODERATE = "moderate"
    AGGRESSIVE = "aggressive"


# Mode-specific clamp bands (multiplicative distance from 1.0)
_MODE_BANDS: Dict[LearningMode, Tuple[float, float]] = {
    LearningMode.CONSERVATIVE: (0.85, 1.15),
    LearningMode.MODERATE:     (0.70, 1.40),
    LearningMode.AGGRESSIVE:   (0.50, 2.00),
}


# ---------------------------------------------------------------------------
# Per-tool metrics bucket
# ---------------------------------------------------------------------------

@dataclass
class ToolCacheMetrics:
    """Tracks cache behaviour for a single tool."""

    hits: int = 0
    misses: int = 0
    total_events: int = 0

    # EWMA of observed call cost (monetary / compute units).
    # Starts as *None*; first observation seeds it.
    cost_ewma: Optional[float] = None

    # EWMA of observed latency (seconds).
    # Starts as *None*; first observation seeds it.
    latency_ewma: Optional[float] = None

    # Timestamp of last event (for recency / staleness checks).
    last_event_ts: Optional[float] = None

    # ---- derived helpers ---------------------------------------------------

    @property
    def hit_rate(self) -> float:
        """Zero-division-safe hit rate; returns neutral 0.5 when no data."""
        if self.total_events == 0:
            return _NEUTRAL_HIT_RATE
        return self.hits / self.total_events

    def record_hit(
        self,
        cost: Optional[float] = None,
        latency: Optional[float] = None,
    ) -> None:
        self.hits += 1
        self.total_events += 1
        self._update_ewmas(cost, latency)

    def record_miss(
        self,
        cost: Optional[float] = None,
        latency: Optional[float] = None,
    ) -> None:
        self.misses += 1
        self.total_events += 1
        self._update_ewmas(cost, latency)

    # ---- internal ----------------------------------------------------------

    def _update_ewmas(
        self,
        cost: Optional[float],
        latency: Optional[float],
    ) -> None:
        now = time.monotonic()
        self.last_event_ts = now

        if cost is not None:
            if self.cost_ewma is None:
                self.cost_ewma = cost  # first observation seeds it
            else:
                self.cost_ewma = (
                    DEFAULT_ALPHA * cost + (1 - DEFAULT_ALPHA) * self.cost_ewma
                )

        if latency is not None:
            if self.latency_ewma is None:
                self.latency_ewma = latency  # first observation seeds it
            else:
                self.latency_ewma = (
                    DEFAULT_ALPHA * latency
                    + (1 - DEFAULT_ALPHA) * self.latency_ewma
                )


# ---------------------------------------------------------------------------
# Recommendation value object
# ---------------------------------------------------------------------------

@dataclass
class TTLRecommendation:
    """Result of a TTL learning computation."""

    tool_name: str
    current_ttl: float
    recommended_ttl: float
    adjustment_factor: float
    applied: bool
    reason: str

    @property
    def is_recommendations_only(self) -> bool:
        """True when the recommendation was *not* auto-applied."""
        return not self.applied


# ---------------------------------------------------------------------------
# TTL Learning engine
# ---------------------------------------------------------------------------

class TTLLearner:
    """Computes per-tool TTL adjustments using a weighted blend of strategies.

    Strategies (base weights, renormalised over whatever is available):
        * Hit-rate   (40 %) – higher hit rate → longer TTL
        * Cost       (40 %) – higher cost → longer TTL (skip until seeded)
        * Recency    (20 %) – higher latency → longer TTL (skip until seeded)

    The blended factor is damped, then clamped by the active
    :class:`LearningMode` band and a global safety range.
    """

    def __init__(
        self,
        mode: LearningMode = LearningMode.MODERATE,
        auto_adjust: bool = True,
        alpha: float = DEFAULT_ALPHA,
        damping: float = DAMPING_FACTOR,
    ) -> None:
        self.mode = mode
        self.auto_adjust = auto_adjust
        self.alpha = alpha
        self.damping = damping
        self._metrics: Dict[str, ToolCacheMetrics] = {}

    # -- public API ----------------------------------------------------------

    def record(
        self,
        tool_name: str,
        hit: bool,
        cost: Optional[float] = None,
        latency: Optional[float] = None,
    ) -> None:
        """Record a cache hit / miss event for *tool_name*."""
        m = self._metrics.setdefault(tool_name, ToolCacheMetrics())
        if hit:
            m.record_hit(cost, latency)
        else:
            m.record_miss(cost, latency)

    def compute_adjustment(
        self,
        tool_name: str,
        current_ttl: float,
    ) -> TTLRecommendation:
        """Compute a TTL recommendation for *tool_name*.

        Always returns a :class:`TTLRecommendation`.  When
        ``auto_adjust`` is *False* the caller should treat the result as
        informational only (``is_recommendations_only`` is *True*).
        """
        m = self._metrics.get(tool_name)

        # -- Cold-start gate -------------------------------------------------
        if m is None or m.total_events < MIN_SAMPLE_GATE:
            return TTLRecommendation(
                tool_name=tool_name,
                current_ttl=current_ttl,
                recommended_ttl=current_ttl,
                adjustment_factor=1.0,
                applied=False,
                reason="cold_start",
            )

        # -- Strategy signals (multiplicative factors) -----------------------

        hit_rate_signal: float = self._hit_rate_signal(m)

        cost_signal: Optional[float] = None
        if m.cost_ewma is not None:
            cost_signal = self._cost_signal(m)

        recency_signal: Optional[float] = None
        if m.latency_ewma is not None and m.last_event_ts is not None:
            recency_signal = self._recency_signal(m)

        # -- Renormalise weights over available strategies --------------------
        raw: List[Tuple[float, Optional[float]]] = [
            (_W_HIT_RATE, hit_rate_signal),
            (_W_COST, cost_signal),
            (_W_RECENCY, recency_signal),
        ]

        available: List[Tuple[float, float]] = [
            (w, s) for w, s in raw if s is not None
        ]

        if not available:
            return TTLRecommendation(
                tool_name=tool_name,
                current_ttl=current_ttl,
                recommended_ttl=current_ttl,
                adjustment_factor=1.0,
                applied=False,
                reason="no_seeded_strategies",
            )

        total_w = sum(w for w, _ in available)
        blended = sum(w * s for w, s in available) / total_w

        # -- Damping (shrink distance from 1.0) ------------------------------
        damped = 1.0 + (blended - 1.0) * self.damping

        # -- Clamp by learning mode ------------------------------------------
        lo, hi = _MODE_BANDS[self.mode]
        clamped = max(lo, min(hi, damped))

        # -- Global safety clamp ---------------------------------------------
        clamped = max(_MIN_ADJUSTMENT, min(_MAX_ADJUSTMENT, clamped))

        recommended_ttl = current_ttl * clamped

        # When auto-adjust is off the recommendation is informational only.
        applied = self.auto_adjust and (clamped != 1.0)
        reason = "adjusted" if clamped != 1.0 else "neutral"

        return TTLRecommendation(
            tool_name=tool_name,
            current_ttl=current_ttl,
            recommended_ttl=recommended_ttl,
            adjustment_factor=clamped,
            applied=applied,
            reason=reason,
        )

    # -- introspection -------------------------------------------------------

    def get_metrics(self, tool_name: str) -> Optional[ToolCacheMetrics]:
        """Return current metrics for *tool_name*, or *None*."""
        return self._metrics.get(tool_name)

    def all_tool_names(self) -> list[str]:
        """Return a list of all tracked tool names."""
        return list(self._metrics.keys())

    def reset_tool(self, tool_name: str) -> None:
        """Discard all metrics for *tool_name*."""
        self._metrics.pop(tool_name, None)

    def reset_all(self) -> None:
        """Discard all metrics."""
        self._metrics.clear()

    # -- strategy helpers (static for clarity) -------------------------------

    @staticmethod
    def _hit_rate_signal(m: ToolCacheMetrics) -> float:
        """Map hit_rate ∈ [0, 1] → multiplier.

        0.0 → 0.6   (decrease TTL)
        0.5 → 1.0   (neutral)
        1.0 → 1.4   (increase TTL)
        """
        hr = m.hit_rate
        return 0.6 + 0.8 * hr

    @staticmethod
    def _cost_signal(m: ToolCacheMetrics) -> float:
        """Higher cost → longer TTL (cache expensive calls).

        Expects *cost_ewma* normalised to [0, 1] by the caller.
        0.0 → 0.7,  0.5 → 1.0,  1.0 → 1.3
        """
        c = max(0.0, min(1.0, m.cost_ewma))  # type: ignore[arg-type]
        return 0.7 + 0.6 * c

    @staticmethod
    def _recency_signal(m: ToolCacheMetrics) -> float:
        """Higher latency → longer TTL (avoid recomputing slow calls).

        Expects *latency_ewma* normalised to [0, 1] by the caller.
        0.0 → 0.8,  0.5 → 1.0,  1.0 → 1.2
        """
        l = max(0.0, min(1.0, m.latency_ewma))  # type: ignore[arg-type]
        return 0.8 + 0.4 * l


# ---------------------------------------------------------------------------
# Config integration
# ---------------------------------------------------------------------------

def learner_from_config(cfg: Any) -> TTLLearner:
    """Build a :class:`TTLLearner` from an arbitrary config object.

    Recognised attributes (all optional, with sensible defaults):
        * ``ttl_learning_mode`` – ``"conservative"`` | ``"moderate"`` | ``"aggressive"``
        * ``ttl_auto_adjust``   – ``bool``
        * ``ttl_ewma_alpha``    – ``float``
        * ``ttl_damping``       – ``float``
    """
    mode_str = getattr(cfg, "ttl_learning_mode", "moderate")
    try:
        mode = LearningMode(mode_str)
    except ValueError:
        mode = LearningMode.MODERATE

    auto = bool(getattr(cfg, "ttl_auto_adjust", True))
    alpha = float(getattr(cfg, "ttl_ewma_alpha", DEFAULT_ALPHA))
    damping = float(getattr(cfg, "ttl_damping", DAMPING_FACTOR))

    return TTLLearner(
        mode=mode,
        auto_adjust=auto,
        alpha=alpha,
        damping=damping,
    )


# ---------------------------------------------------------------------------
# Tool-call-path wrapper
# ---------------------------------------------------------------------------

def tool_call_wrapper(
    learner: TTLLearner,
    tool_name: str,
    current_ttl: float,
    call_fn: Any,
    *args: Any,
    **kwargs: Any,
) -> Tuple[Any, TTLRecommendation]:
    """Drop-in wrapper for a tool-call path.

    *call_fn* must return ``(result, hit, cost=None, latency=None)`` where
    ``hit`` is a :class:`bool` indicating whether the result came from
    cache.

    The wrapper records the event, computes a TTL recommendation, and
    returns ``(result, recommendation)``.  When ``auto_adjust`` is off
    the recommendation carries ``applied=False`` and should be treated
    as informational (recommendations-only mode).
    """
    result = call_fn(*args, **kwargs)

    # Unpack the call_fn return value.
    if not isinstance(result, tuple) or len(result) < 2:
        raise TypeError(
            "call_fn must return (result, hit, [cost], [latency])"
        )

    actual_result = result[0]
    hit = bool(result[1])
    cost = result[2] if len(result) > 2 else None
    latency = result[3] if len(result) > 3 else None

    learner.record(tool_name, hit=hit, cost=cost, latency=latency)
    rec = learner.compute_adjustment(tool_name, current_ttl)

    return actual_result, rec
