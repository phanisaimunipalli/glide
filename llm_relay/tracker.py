"""
Per-model latency tracker.

Maintains a rolling window of observed TTFT (time-to-first-token) values
for each model and computes the p95 latency.

Used for proactive routing: if a model's p95 TTFT is already above its
budget, skip it rather than waiting for it to timeout again.
"""

import logging
from collections import deque
from typing import Dict, Optional

from .config import settings

logger = logging.getLogger("llm_relay.tracker")


class ModelLatencyTracker:
    """Tracks TTFT samples for a single model."""

    def __init__(self, window_size: int = 20):
        self._window: deque = deque(maxlen=window_size)

    def record(self, ttft: float):
        self._window.append(ttft)
        logger.debug(f"Recorded TTFT {ttft:.2f}s (window={len(self._window)})")

    def p95(self) -> Optional[float]:
        """Returns p95 TTFT, or None if fewer than 5 samples."""
        if len(self._window) < 5:
            return None
        sorted_w = sorted(self._window)
        idx = int(len(sorted_w) * 0.95)
        return sorted_w[min(idx, len(sorted_w) - 1)]

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
            logger.info(f"Proactive skip — p95={p:.2f}s > budget={ttft_budget}s")
        return skip

    @property
    def sample_count(self) -> int:
        return len(self._window)

    def stats(self) -> dict:
        if not self._window:
            return {"samples": 0, "p95": None, "mean": None}
        sorted_w = sorted(self._window)
        return {
            "samples": len(self._window),
            "p95": self.p95(),
            "mean": round(sum(self._window) / len(self._window), 3),
            "min": round(sorted_w[0], 3),
            "max": round(sorted_w[-1], 3),
        }


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
