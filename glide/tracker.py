"""
Per-model latency tracker.

Tracks two signals per model:
  TTFT — time-to-first-token: time until first byte arrives from the model.
          Used to detect slow starts and connection issues.
  TTT  — time-to-think: time from request start until the first *text* token
          appears, i.e. after any thinking/reasoning block completes.
          Only meaningful for models with extended thinking (opus, o1, etc.).
          For models without thinking, TTT == TTFT.

Both use a rolling window of recent samples to compute p95. Proactive routing
skips a model if its p95 TTFT already exceeds the configured budget.
"""

import logging
from collections import deque
from typing import Dict, Optional

from .config import settings

logger = logging.getLogger("glide.tracker")


class ModelLatencyTracker:
    """Tracks TTFT and TTT samples for a single model."""

    def __init__(self, window_size: int = 20):
        self._ttft: deque = deque(maxlen=window_size)
        self._ttt: deque = deque(maxlen=window_size)

    # ------------------------------------------------------------------
    # Record

    def record(self, ttft: float):
        """Backward-compatible alias for record_ttft."""
        self.record_ttft(ttft)

    def record_ttft(self, v: float):
        self._ttft.append(v)
        logger.debug(f"Recorded TTFT {v:.2f}s (window={len(self._ttft)})")

    def record_ttt(self, v: float):
        self._ttt.append(v)
        logger.debug(f"Recorded TTT  {v:.2f}s (window={len(self._ttt)})")

    # ------------------------------------------------------------------
    # TTFT stats

    def p95(self) -> Optional[float]:
        """Returns p95 TTFT, or None if fewer than 5 samples."""
        return _p95(self._ttft)

    def should_skip(self, ttft_budget: Optional[float]) -> bool:
        """
        Returns True if this model's p95 TTFT exceeds the budget,
        meaning it will likely timeout again — skip it proactively.
        """
        if not settings.proactive_skip or ttft_budget is None:
            return False
        p = self.p95()
        if p is None:
            return False
        skip = p > ttft_budget
        if skip:
            logger.info(f"Proactive skip — p95_ttft={p:.2f}s > budget={ttft_budget}s")
        return skip

    # ------------------------------------------------------------------
    # TTT stats

    def ttt_p95(self) -> Optional[float]:
        """Returns p95 TTT, or None if fewer than 5 samples."""
        return _p95(self._ttt)

    def should_skip_ttt(self, ttt_budget: Optional[float]) -> bool:
        """
        Returns True if this model's p95 TTT exceeds the ttt_budget.
        Used for proactive routing when thinking is consistently too slow.
        """
        if not settings.proactive_skip or ttt_budget is None:
            return False
        p = self.ttt_p95()
        if p is None:
            return False
        skip = p > ttt_budget
        if skip:
            logger.info(f"Proactive skip — p95_ttt={p:.2f}s > ttt_budget={ttt_budget}s")
        return skip

    # ------------------------------------------------------------------

    @property
    def sample_count(self) -> int:
        return len(self._ttft)

    def stats(self) -> dict:
        return {
            "ttft": _window_stats(self._ttft, self.p95()),
            "ttt":  _window_stats(self._ttt,  self.ttt_p95()),
        }


# ---------------------------------------------------------------------------
# Helpers

def _p95(window: deque) -> Optional[float]:
    if len(window) < 5:
        return None
    sorted_w = sorted(window)
    idx = int(len(sorted_w) * 0.95)
    return sorted_w[min(idx, len(sorted_w) - 1)]


def _window_stats(window: deque, p95_val: Optional[float]) -> dict:
    if not window:
        return {"samples": 0, "p95": None, "mean": None}
    sorted_w = sorted(window)
    return {
        "samples": len(window),
        "p95": p95_val,
        "mean": round(sum(window) / len(window), 3),
        "min": round(sorted_w[0], 3),
        "max": round(sorted_w[-1], 3),
    }


# ---------------------------------------------------------------------------

class TrackerRegistry:
    """Global registry of per-model trackers."""

    def __init__(self):
        self._trackers: Dict[str, ModelLatencyTracker] = {}

    def get(self, model: str) -> ModelLatencyTracker:
        if model not in self._trackers:
            self._trackers[model] = ModelLatencyTracker(
                window_size=settings.tracker_window
            )
        return self._trackers[model]

    def all_stats(self) -> dict:
        return {model: t.stats() for model, t in self._trackers.items()}


registry = TrackerRegistry()
