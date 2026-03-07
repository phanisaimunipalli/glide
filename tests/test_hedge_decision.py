"""
Tests for _hedge_decision() — the smart hedge trigger.

Verifies all three outcomes:
  solo  — model 1 healthy (p95 < budget * 0.8)
  hedge — model 1 risky, model 2 healthy or cold
  skip  — both models risky
"""
import pytest
from unittest.mock import patch, MagicMock
from glide.cascade import _hedge_decision
from glide.config import ModelConfig


def make_model(name, ttft_budget):
    return ModelConfig(provider="anthropic", model=name, ttft_budget=ttft_budget)


def make_tracker(p95_val):
    t = MagicMock()
    t.p95.return_value = p95_val
    return t


@pytest.fixture
def models():
    return [
        make_model("claude-opus-4-6",   ttft_budget=4.0),
        make_model("claude-sonnet-4-6", ttft_budget=5.0),
    ]


def mock_registry(p95_opus, p95_sonnet):
    """Return a mock registry with preset p95 values."""
    reg = MagicMock()
    def get(model):
        if "opus" in model:
            return make_tracker(p95_opus)
        return make_tracker(p95_sonnet)
    reg.get.side_effect = get
    return reg


# ── solo ────────────────────────────────────────────────────────────────────

def test_solo_when_opus_healthy(models):
    """opus p95=1.0s, budget=4s, threshold=3.2s → solo."""
    with patch("glide.cascade.registry", mock_registry(p95_opus=1.0, p95_sonnet=1.0)):
        assert _hedge_decision(models) == "solo"


def test_solo_exactly_at_threshold_is_solo(models):
    """p95 just below threshold (3.19s < 3.2s) → solo."""
    with patch("glide.cascade.registry", mock_registry(p95_opus=3.19, p95_sonnet=1.0)):
        assert _hedge_decision(models) == "solo"


# ── hedge ───────────────────────────────────────────────────────────────────

def test_hedge_when_opus_risky_sonnet_healthy(models):
    """opus p95=3.5s ≥ 3.2s threshold, sonnet healthy → hedge."""
    with patch("glide.cascade.registry", mock_registry(p95_opus=3.5, p95_sonnet=1.0)):
        assert _hedge_decision(models) == "hedge"


def test_hedge_when_opus_cold(models):
    """opus has no p95 data yet (cold start) → hedge conservatively."""
    with patch("glide.cascade.registry", mock_registry(p95_opus=None, p95_sonnet=1.0)):
        assert _hedge_decision(models) == "hedge"


def test_hedge_when_both_cold(models):
    """Both cold → hedge (we don't know anything, default conservative)."""
    with patch("glide.cascade.registry", mock_registry(p95_opus=None, p95_sonnet=None)):
        assert _hedge_decision(models) == "hedge"


def test_hedge_when_opus_risky_sonnet_cold(models):
    """opus risky, sonnet cold (None) → hedge (sonnet might be fine)."""
    with patch("glide.cascade.registry", mock_registry(p95_opus=4.5, p95_sonnet=None)):
        assert _hedge_decision(models) == "hedge"


# ── skip ────────────────────────────────────────────────────────────────────

def test_skip_when_both_risky(models):
    """opus p95=4.5s ≥ 3.2s, sonnet p95=5.5s ≥ 4.0s → skip to sequential."""
    with patch("glide.cascade.registry", mock_registry(p95_opus=4.5, p95_sonnet=5.5)):
        assert _hedge_decision(models) == "skip"


def test_skip_boundary(models):
    """Both right at their thresholds → skip."""
    # opus threshold = 4.0 * 0.8 = 3.2s
    # sonnet threshold = 5.0 * 0.8 = 4.0s
    with patch("glide.cascade.registry", mock_registry(p95_opus=3.2, p95_sonnet=4.0)):
        assert _hedge_decision(models) == "skip"


# ── single-model edge case ───────────────────────────────────────────────────

def test_single_model_risky_no_second(models):
    """Only one model in hedge list and it's risky → hedge (no second to compare)."""
    single = [models[0]]
    with patch("glide.cascade.registry", mock_registry(p95_opus=5.0, p95_sonnet=None)):
        assert _hedge_decision(single) == "hedge"
