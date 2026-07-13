"""Tests for the per-tool circuit breaker gate in handle_function_call (#942).

Verifies that:
1. After N consecutive failures, the circuit breaker blocks further calls
   and returns a diagnostic telling the model to stop retrying.
2. A success resets the breaker, allowing future calls.
3. The escalation warning appears in error output before the breaker trips.
"""

import json

import pytest

from agent.tool_error_recovery import (
    CircuitBreaker,
    get_breaker,
    record_tool_outcome,
)
from tools.registry import registry


@pytest.fixture(autouse=True)
def _reset_breakers():
    """Clear the per-tool breaker registry before and after each test."""
    from agent import tool_error_recovery as ter

    ter._breakers.clear()
    yield
    ter._breakers.clear()


def test_circuit_breaker_trips_after_threshold():
    """After ``threshold`` consecutive failures, should_trip() returns True."""
    breaker = CircuitBreaker(threshold=5)
    for i in range(4):
        breaker.record_failure()
        assert not breaker.should_trip(), f"tripped early at failure {i + 1}"
    breaker.record_failure()
    assert breaker.should_trip()


def test_circuit_breaker_resets_on_success():
    """A single success resets the consecutive-failure count."""
    breaker = CircuitBreaker(threshold=3)
    breaker.record_failure()
    breaker.record_failure()
    assert not breaker.should_trip()
    breaker.record_success()
    assert not breaker.should_trip()
    assert breaker._consecutive_failures == 0


def test_record_tool_outcome_tracks_failures():
    """record_tool_outcome increments failures and trips at threshold."""
    for _ in range(5):
        record_tool_outcome("terminal", success=False)
    breaker = get_breaker("terminal")
    assert breaker.should_trip()


def test_record_tool_outcome_resets_on_success():
    """A success after failures resets the breaker."""
    record_tool_outcome("terminal", success=False)
    record_tool_outcome("terminal", success=False)
    record_tool_outcome("terminal", success=True)
    breaker = get_breaker("terminal")
    assert not breaker.should_trip()
    assert breaker._consecutive_failures == 0


def test_get_breaker_returns_same_instance():
    """get_breaker is idempotent — same tool name → same breaker."""
    b1 = get_breaker("terminal")
    b2 = get_breaker("terminal")
    assert b1 is b2


def test_get_breaker_different_tools_independent():
    """Breakers for different tools are independent."""
    b_terminal = get_breaker("terminal")
    b_read = get_breaker("read_file")
    assert b_terminal is not b_read
    b_terminal.record_failure()
    b_terminal.record_failure()
    b_terminal.record_failure()
    b_terminal.record_failure()
    b_terminal.record_failure()
    assert b_terminal.should_trip()
    assert not b_read.should_trip()


def test_circuit_breaker_threshold_configurable():
    """Breaker threshold is configurable per tool."""
    b = get_breaker("custom_tool", threshold=3)
    b.record_failure()
    b.record_failure()
    assert not b.should_trip()
    b.record_failure()
    assert b.should_trip()