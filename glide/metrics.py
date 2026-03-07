"""
Prometheus-compatible metrics for glide.

Exposes a /metrics endpoint in standard Prometheus text format.
No external dependencies — formatted manually.

Metrics:
  glide_requests_total                    Counter  total requests handled
  glide_hedge_decision_total{decision}    Counter  solo / hedge / skip decisions
  glide_hedge_winner_total{model}         Counter  which model won each hedge race
  glide_cascade_fallback_total            Counter  hedge failures → sequential cascade
  glide_ttft_p95_seconds{model}          Gauge    rolling p95 TTFT per model
  glide_ttt_p95_seconds{model}           Gauge    rolling p95 TTT per model
  glide_ttft_samples_total{model}        Gauge    TTFT sample count per model
  glide_ttt_samples_total{model}         Gauge    TTT sample count per model
"""

import threading
from collections import defaultdict
from typing import Dict


class _Counter:
    def __init__(self):
        self._lock = threading.Lock()
        self._values: Dict[tuple, float] = defaultdict(float)

    def inc(self, labels: tuple = (), amount: float = 1.0):
        with self._lock:
            self._values[labels] += amount

    def items(self):
        with self._lock:
            return list(self._values.items())


class MetricsRegistry:
    def __init__(self):
        self.requests_total       = _Counter()
        self.hedge_decision_total = _Counter()  # labels: (decision,)
        self.hedge_winner_total   = _Counter()  # labels: (model,)
        self.cascade_fallback_total = _Counter()

    def record_request(self):
        self.requests_total.inc()

    def record_hedge_decision(self, decision: str):
        self.hedge_decision_total.inc((decision,))

    def record_hedge_winner(self, model: str):
        self.hedge_winner_total.inc((model,))

    def record_cascade_fallback(self):
        self.cascade_fallback_total.inc()

    def render(self, tracker_registry) -> str:
        """Render all metrics in Prometheus text format."""
        lines = []

        def gauge(name, help_text, value, labels: dict = None):
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} gauge")
            label_str = _fmt_labels(labels) if labels else ""
            lines.append(f"{name}{label_str} {_fmt_value(value)}")

        def counter(name, help_text, items):
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} counter")
            for label_tuple, value in items:
                if label_tuple:
                    label_str = _fmt_labels_from_tuple(label_tuple)
                else:
                    label_str = ""
                lines.append(f"{name}{label_str} {_fmt_value(value)}")
            if not items:
                lines.append(f"{name} 0")

        # Counters
        counter("glide_requests_total",
                "Total requests handled by glide",
                self.requests_total.items())

        counter("glide_hedge_decision_total",
                "Hedge routing decisions (solo, hedge, skip)",
                [({("decision", t[0])}, v) for t, v in self.hedge_decision_total.items()])

        counter("glide_hedge_winner_total",
                "Models that won a hedge race",
                [({("model", t[0])}, v) for t, v in self.hedge_winner_total.items()])

        counter("glide_cascade_fallback_total",
                "Times hedge failed and fell through to sequential cascade",
                self.cascade_fallback_total.items())

        # Per-model gauges from tracker registry
        lines.append("# HELP glide_ttft_p95_seconds Rolling p95 TTFT per model (seconds)")
        lines.append("# TYPE glide_ttft_p95_seconds gauge")
        lines.append("# HELP glide_ttt_p95_seconds Rolling p95 TTT per model (seconds)")
        lines.append("# TYPE glide_ttt_p95_seconds gauge")
        lines.append("# HELP glide_ttft_samples_total TTFT sample count per model")
        lines.append("# TYPE glide_ttft_samples_total gauge")
        lines.append("# HELP glide_ttt_samples_total TTT sample count per model")
        lines.append("# TYPE glide_ttt_samples_total gauge")

        for model, tracker in tracker_registry._trackers.items():
            s = tracker.stats()
            lbl = _fmt_labels({"model": model})
            ttft = s["ttft"]
            ttt  = s["ttt"]
            lines.append(f'glide_ttft_p95_seconds{lbl} {_fmt_value(ttft.get("p95"))}')
            lines.append(f'glide_ttt_p95_seconds{lbl} {_fmt_value(ttt.get("p95"))}')
            lines.append(f'glide_ttft_samples_total{lbl} {ttft.get("samples", 0)}')
            lines.append(f'glide_ttt_samples_total{lbl} {ttt.get("samples", 0)}')

        return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Helpers

def _fmt_value(v) -> str:
    if v is None:
        return "NaN"
    return str(round(v, 6))


def _fmt_labels(d: dict) -> str:
    parts = ', '.join(f'{k}="{v}"' for k, v in d.items())
    return "{" + parts + "}"


def _fmt_labels_from_tuple(t: tuple) -> str:
    # t is a dict passed from counter items
    if isinstance(t, dict):
        return _fmt_labels(t)
    return ""


# Singleton
metrics = MetricsRegistry()
