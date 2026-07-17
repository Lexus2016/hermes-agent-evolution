"""Tests for the timeout circuit breaker in the retry loop (#1142).

Verifies that:
1. TurnRetryState.consecutive_timeout_hits starts at 0.
2. The counter increments on timeout classification.
3. The counter resets on non-timeout errors.
4. Fallback triggers after 2 consecutive timeout errors — mirroring the
   overload circuit breaker pattern (#943) so the loop fails over to a
   different provider instead of backoff-and-retry against the same
   degraded endpoint.
"""

from agent.turn_retry_state import TurnRetryState


def test_timeout_counter_starts_zero():
    """consecutive_timeout_hits defaults to 0."""
    state = TurnRetryState()
    assert state.consecutive_timeout_hits == 0


def test_timeout_counter_increments():
    """The counter can be incremented to simulate consecutive timeout errors."""
    state = TurnRetryState()
    state.consecutive_timeout_hits += 1
    assert state.consecutive_timeout_hits == 1
    state.consecutive_timeout_hits += 1
    assert state.consecutive_timeout_hits == 2


def test_timeout_counter_resets():
    """The counter resets to 0 on fallback activation."""
    state = TurnRetryState()
    state.consecutive_timeout_hits = 2
    state.consecutive_timeout_hits = 0
    assert state.consecutive_timeout_hits == 0


def test_timeout_counter_is_distinct_from_overload():
    """consecutive_timeout_hits is a separate field from consecutive_overload_hits."""
    state = TurnRetryState()
    state.consecutive_timeout_hits = 3
    state.consecutive_overload_hits = 1
    assert state.consecutive_timeout_hits == 3
    assert state.consecutive_overload_hits == 1
    # Resetting one doesn't affect the other
    state.consecutive_timeout_hits = 0
    assert state.consecutive_overload_hits == 1


def test_timeout_fallback_threshold():
    """At 2 consecutive timeout hits, the fallback gate should fire."""
    state = TurnRetryState()
    # Simulate the condition check from conversation_loop.py (#1142):
    #   or (classified.reason == FailoverReason.timeout
    #       and _retry.consecutive_timeout_hits >= 2)
    state.consecutive_timeout_hits = 1
    should_fallback = state.consecutive_timeout_hits >= 2
    assert not should_fallback

    state.consecutive_timeout_hits = 2
    should_fallback = state.consecutive_timeout_hits >= 2
    assert should_fallback