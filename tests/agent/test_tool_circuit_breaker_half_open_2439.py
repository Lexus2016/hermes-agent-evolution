"""Tests for half-open probe recovery of tool circuit breakers (#2439).

The old breaker, once open, stayed open for the whole session because the
gate in ``handle_function_call`` refused the call *before* dispatch, so
``record_success()`` was unreachable. This suite verifies the three-state
machine (closed → open → half-open → closed / open) and the single-probe
semantics of the cooldown-based half-open transition.
"""

import time

import pytest

from agent.tool_error_recovery import CircuitBreaker, get_breaker, record_tool_outcome


@pytest.fixture(autouse=True)
def _reset_breakers():
    from agent import tool_error_recovery as ter

    ter._breakers.clear()
    yield
    ter._breakers.clear()


def _trip(breaker: CircuitBreaker, threshold: int = 5) -> None:
    for _ in range(threshold):
        breaker.record_failure()


def test_open_breaker_blocks_during_cooldown():
    """While cooling down, should_trip() keeps refusing the call."""
    breaker = CircuitBreaker(threshold=3, cooldown_s=30.0)
    _trip(breaker, threshold=3)
    assert breaker.should_trip() is True
    assert breaker.should_trip() is True  # still within cooldown


def test_half_open_admits_single_probe_after_cooldown():
    """After the cooldown elapses, exactly one call is admitted as a probe."""
    breaker = CircuitBreaker(threshold=3, cooldown_s=0.0)
    _trip(breaker, threshold=3)
    # cooldown 0 → immediately half-open on first should_trip
    assert breaker.should_trip() is False  # the probe is admitted
    # concurrent callers are still blocked while the probe is in flight
    assert breaker.should_trip() is True


def test_probe_success_recloses_breaker():
    """A successful probe closes the breaker and clears the failure count."""
    breaker = CircuitBreaker(threshold=3, cooldown_s=0.0)
    _trip(breaker, threshold=3)
    assert breaker.should_trip() is False  # probe admitted
    breaker.record_success()  # probe outcome: success
    assert breaker.should_trip() is False  # closed → not tripped
    assert breaker._consecutive_failures == 0
    assert breaker._probe_in_flight is False


def test_probe_failure_reopens_with_fresh_cooldown():
    """A failed probe reopens the breaker and restarts the cooldown."""
    breaker = CircuitBreaker(threshold=3, cooldown_s=0.0)
    _trip(breaker, threshold=3)
    assert breaker.should_trip() is False  # probe admitted
    breaker.record_failure()  # probe outcome: failed
    # reopened → still open; with cooldown 0 the next should_trip re-admits
    assert breaker._is_open is True
    assert breaker.should_trip() is False  # new probe after fresh cooldown


def test_closed_breaker_never_blocks():
    """A breaker that has not tripped admits every call."""
    breaker = CircuitBreaker(threshold=5)
    for _ in range(4):
        assert breaker.should_trip() is False


def test_record_tool_outcome_recovers_after_half_open_probe():
    """End-to-end: a tool that trips, then succeeds on a probe, recovers."""
    for _ in range(5):
        record_tool_outcome("terminal", success=False)
    breaker = get_breaker("terminal")
    breaker.cooldown_s = 0.0  # simulate cooldown having elapsed
    assert breaker.should_trip() is False  # probe admitted
    record_tool_outcome("terminal", success=True)  # probe succeeds
    assert breaker.should_trip() is False  # fully recovered


def test_open_breaker_is_wedged_without_cooldown_elapsing():
    """Regression guard: with a finite cooldown, an open breaker stays open."""
    breaker = CircuitBreaker(threshold=2, cooldown_s=3600.0)
    _trip(breaker, threshold=2)
    # A full hour of cooldown → no probe admitted
    assert breaker.should_trip() is True
    assert breaker.should_trip() is True
    assert breaker._probe_in_flight is False


def test_opened_at_is_recorded_when_tripping():
    """Tripping stamps _opened_at so cooldown measurement has a baseline."""
    breaker = CircuitBreaker(threshold=2)
    before = time.monotonic()
    _trip(breaker, threshold=2)
    assert breaker._opened_at is not None
    assert before <= breaker._opened_at <= time.monotonic()
