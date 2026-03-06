"""
Tests for the per-model latency tracker.
"""

import pytest
from llm_relay.tracker import ModelLatencyTracker


@pytest.fixture
def tracker():
    return ModelLatencyTracker(window_size=10)


def test_initial_state(tracker):
    assert tracker.sample_count == 0
    assert tracker.p95() is None


def test_p95_requires_minimum_samples(tracker):
    for t in [1.0, 2.0, 3.0, 4.0]:
        tracker.record(t)
    assert tracker.p95() is None  # need at least 5


def test_p95_with_enough_samples(tracker):
    for t in [1.0, 1.0, 1.0, 1.0, 10.0]:
        tracker.record(t)
    # p95 should reflect the high outlier
    assert tracker.p95() == 10.0


def test_p95_normal_distribution(tracker):
    for t in [1.0, 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8, 1.9]:
        tracker.record(t)
    p = tracker.p95()
    assert p is not None
    assert 1.8 <= p <= 1.9


def test_window_is_rolling(tracker):
    # Fill window with slow times
    for _ in range(10):
        tracker.record(9.0)
    # Now fill with fast times — old slow samples should be evicted
    for _ in range(10):
        tracker.record(1.0)
    assert tracker.p95() == 1.0


def test_should_skip_no_data(tracker):
    # Without enough data, should never skip
    assert not tracker.should_skip(ttft_budget=5.0)


def test_should_skip_below_budget(tracker):
    for _ in range(5):
        tracker.record(2.0)
    assert not tracker.should_skip(ttft_budget=5.0)


def test_should_skip_above_budget(tracker):
    for _ in range(5):
        tracker.record(8.0)
    assert tracker.should_skip(ttft_budget=5.0)


def test_should_skip_no_budget(tracker):
    for _ in range(5):
        tracker.record(99.0)
    # No budget = never skip (last resort model)
    assert not tracker.should_skip(ttft_budget=None)


def test_stats_structure(tracker):
    for t in [1.0, 2.0, 3.0, 4.0, 5.0]:
        tracker.record(t)
    s = tracker.stats()
    assert s["samples"] == 5
    assert s["p95"] is not None
    assert s["mean"] == 3.0
    assert s["min"] == 1.0
    assert s["max"] == 5.0
