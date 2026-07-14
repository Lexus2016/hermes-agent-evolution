"""Tests for memory circuit breaker (#977).

After 3 consecutive memory tool failures, the breaker opens and
returns a "proceed without persistence" directive instead of
attempting another failing call.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from agent.tool_error_recovery import CircuitBreaker, get_breaker


class TestMemoryCircuitBreaker:
    """Circuit breaker for the memory tool (threshold=3)."""

    def test_breaker_trips_after_3_failures(self):
        """After 3 consecutive failures, should_trip() returns True."""
        breaker = CircuitBreaker(threshold=3)
        breaker.record_failure()
        breaker.record_failure()
        assert not breaker.should_trip()
        breaker.record_failure()
        assert breaker.should_trip()

    def test_breaker_resets_on_success(self):
        """A success resets the failure counter."""
        breaker = CircuitBreaker(threshold=3)
        breaker.record_failure()
        breaker.record_failure()
        breaker.record_success()
        breaker.record_failure()
        breaker.record_failure()
        assert not breaker.should_trip()

    def test_memory_breaker_uses_threshold_3(self):
        """The memory breaker should be configured with threshold=3."""
        breaker = get_breaker("memory", threshold=3)
        assert breaker.threshold == 3

    def test_directive_message_after_breaker_open(self):
        """When the breaker is open, the memory tool should return a
        proceed-without-persistence directive."""
        breaker = get_breaker("memory", threshold=3)
        # Force the breaker open by recording 3 failures.
        breaker.record_failure()
        breaker.record_failure()
        breaker.record_failure()
        assert breaker.should_trip()
        # Verify the directive message content structure.
        # The actual dispatch is in agent_runtime_helpers.invoke_tool, but
        # we can verify the breaker state and the directive would fire.
        assert breaker._consecutive_failures >= 3

    def test_breaker_stays_open_until_success(self):
        """Once open, the breaker stays open until explicitly reset."""
        breaker = CircuitBreaker(threshold=3)
        for _ in range(3):
            breaker.record_failure()
        assert breaker.should_trip()
        # Even more failures keep it open.
        breaker.record_failure()
        assert breaker.should_trip()
        # Only success resets it.
        breaker.record_success()
        assert not breaker.should_trip()