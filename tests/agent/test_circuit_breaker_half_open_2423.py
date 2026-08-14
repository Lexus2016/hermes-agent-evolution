"""Half-open recovery tests for the per-tool circuit breaker (#2423).

Cooldown expiry is forced by back-dating ``_opened_at``, never sleeping.
"""

import pytest

from agent.tool_error_recovery import CircuitBreaker, get_breaker, record_tool_outcome

requires_half_open = pytest.mark.skipif(
    not hasattr(CircuitBreaker, "seconds_until_retry"),
    reason="circuit breaker half-open recovery (#2423) not on this tree",
)


@requires_half_open
class TestHalfOpenLifecycle:
    def test_trips_repeatedly_during_cooldown(self):
        cb = CircuitBreaker(threshold=2, cooldown_seconds=3600.0)
        cb.record_failure()
        cb.record_failure()
        assert cb.should_trip()
        assert cb.should_trip()  # still cooling down: no probe admitted
        assert not cb.is_half_open()

    def test_admits_exactly_one_probe_after_cooldown(self):
        cb = CircuitBreaker(threshold=2, cooldown_seconds=60.0)
        cb.record_failure()
        cb.record_failure()
        assert cb.should_trip()  # opened; still cooling down → trips
        cb._opened_at -= 61.0  # force cooldown expiry without sleeping
        assert cb.should_trip() is False  # the one admitted probe
        assert cb.is_half_open()
        assert cb.should_trip()  # further calls trip until the outcome lands

    def test_successful_probe_closes_breaker(self):
        cb = CircuitBreaker(threshold=2, cooldown_seconds=60.0)
        cb.record_failure()
        cb.record_failure()
        cb._opened_at -= 61.0
        assert cb.should_trip() is False  # probe admitted
        cb.record_success()
        assert not cb.should_trip()
        assert not cb.is_half_open()
        assert cb._consecutive_failures == 0

    def test_failed_probe_reopens_with_fresh_cooldown(self):
        cb = CircuitBreaker(threshold=2, cooldown_seconds=60.0)
        cb.record_failure()
        cb.record_failure()
        cb._opened_at -= 61.0
        assert cb.should_trip() is False  # probe admitted
        cb.record_failure()  # probe failed → re-open, fresh cooldown
        assert cb.should_trip()
        assert not cb.is_half_open()
        cb._opened_at -= 61.0
        assert cb.should_trip() is False  # new probe after fresh cooldown

    def test_stale_probe_is_rearmed(self):
        cb = CircuitBreaker(threshold=2, cooldown_seconds=60.0)
        cb.record_failure()
        cb.record_failure()
        cb._opened_at -= 61.0
        assert cb.should_trip() is False  # probe admitted but never recorded
        cb._probe_started_at -= 400.0  # outcome lost (cancelled call) → stale
        assert cb.should_trip() is False  # re-armed, not tripping forever

    def test_seconds_until_retry_counts_down(self):
        cb = CircuitBreaker(threshold=1, cooldown_seconds=60.0)
        cb.record_failure()
        assert 0.0 < cb.seconds_until_retry() <= 60.0
        cb._opened_at -= 60.0
        assert cb.seconds_until_retry() == 0.0


@requires_half_open
class TestHalfOpenRegistry:
    def test_record_outcome_success_closes_half_open(self):
        name = "test_tool_halfopen_2423_a"
        breaker = get_breaker(name, threshold=2)
        breaker.cooldown_seconds = 60.0
        record_tool_outcome(name, success=False)
        record_tool_outcome(name, success=False)
        breaker._opened_at -= 61.0
        assert breaker.should_trip() is False  # probe admitted
        record_tool_outcome(name, success=True)
        assert not breaker.should_trip()

    def test_record_outcome_failure_during_probe_reopens(self):
        name = "test_tool_halfopen_2423_b"
        breaker = get_breaker(name, threshold=2)
        breaker.cooldown_seconds = 60.0
        record_tool_outcome(name, success=False)
        record_tool_outcome(name, success=False)
        breaker._opened_at -= 61.0
        assert breaker.should_trip() is False  # probe admitted
        record_tool_outcome(name, success=False)  # probe failed → re-open
        assert breaker.should_trip()  # fresh 60s cooldown → trips
        assert breaker._consecutive_failures == 3
