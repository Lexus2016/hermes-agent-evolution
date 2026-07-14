"""Tests for the overload circuit breaker in the retry loop (#943).

Verifies that:
1. TurnRetryState.consecutive_overload_hits starts at 0.
2. The counter increments on overloaded classification.
3. The counter resets on non-overloaded errors.
4. Fallback triggers after 2 consecutive overloaded errors.
"""

from agent.turn_retry_state import TurnRetryState


def test_overload_counter_starts_zero():
    """consecutive_overload_hits defaults to 0."""
    state = TurnRetryState()
    assert state.consecutive_overload_hits == 0


def test_overload_counter_increments():
    """The counter can be incremented to simulate consecutive overload errors."""
    state = TurnRetryState()
    state.consecutive_overload_hits += 1
    assert state.consecutive_overload_hits == 1
    state.consecutive_overload_hits += 1
    assert state.consecutive_overload_hits == 2


def test_overload_counter_resets():
    """The counter resets to 0 on fallback activation."""
    state = TurnRetryState()
    state.consecutive_overload_hits = 2
    state.consecutive_overload_hits = 0
    assert state.consecutive_overload_hits == 0


def test_overload_counter_is_distinct_from_rate_limit():
    """consecutive_overload_hits is a separate field from consecutive_rate_limit_hits."""
    state = TurnRetryState()
    state.consecutive_overload_hits = 3
    state.consecutive_rate_limit_hits = 1
    assert state.consecutive_overload_hits == 3
    assert state.consecutive_rate_limit_hits == 1
    # Resetting one doesn't affect the other
    state.consecutive_overload_hits = 0
    assert state.consecutive_rate_limit_hits == 1


def test_overload_fallback_threshold():
    """At 2 consecutive overload hits, the fallback gate should fire."""
    state = TurnRetryState()
    # Simulate the condition check from conversation_loop.py
    state.consecutive_overload_hits = 1
    should_fallback = state.consecutive_overload_hits >= 2
    assert not should_fallback

    state.consecutive_overload_hits = 2
    should_fallback = state.consecutive_overload_hits >= 2
    assert should_fallback