"""Regression tests for #2439 — circuit breaker half-open recovery.

The breaker used to open permanently for the session: once tripped,
``should_trip()`` blocked every subsequent call forever, because the block
itself prevented the success that would have reset the breaker. These tests
assert the full recovery cycle required by the issue:

    open (N consecutive failures) -> cooldown -> single half-open probe
    -> closes on a successful probe / re-opens on a failed probe.

They are written against the post-fix ``CircuitBreaker`` API
(``cooldown_s``, ``state``, ``seconds_until_probe``) and fail on the
pre-fix implementation.
"""
from __future__ import annotations

import threading
import time

import pytest

from agent import tool_error_recovery as ter
from agent.tool_error_recovery import (
    CircuitBreaker,
    get_breaker,
    record_tool_outcome,
)

# Tolerant timing constants: cooldown long enough that a slow CI runner
# cannot accidentally cross it before the "still blocked" assertions, and
# WAIT long enough that the cooldown has reliably elapsed afterwards.
COOLDOWN_S = 0.25
WAIT_S = 0.35


@pytest.fixture(autouse=True)
def _clean_global_breakers():
    ter._breakers.clear()
    yield
    ter._breakers.clear()


def _trip(breaker: CircuitBreaker, times: int) -> None:
    for _ in range(times):
        breaker.record_failure()


class TestOpensAfterThreshold:
    def test_closed_by_default(self):
        assert CircuitBreaker().should_trip() is False

    def test_opens_after_threshold_consecutive_failures(self):
        breaker = CircuitBreaker(threshold=5, cooldown_s=COOLDOWN_S)
        for i in range(4):
            breaker.record_failure()
            assert breaker.should_trip() is False, f"tripped early at {i + 1} failures"
        breaker.record_failure()
        assert breaker.should_trip() is True

    def test_success_resets_failure_count_while_closed(self):
        breaker = CircuitBreaker(threshold=3, cooldown_s=COOLDOWN_S)
        breaker.record_failure()
        breaker.record_failure()
        breaker.record_success()
        breaker.record_failure()
        assert breaker.should_trip() is False


class TestHalfOpenRecovery:
    """The core #2439 scenario: open -> cooldown -> probe -> close."""

    def _opened_breaker(self) -> CircuitBreaker:
        breaker = CircuitBreaker(threshold=3, cooldown_s=COOLDOWN_S)
        _trip(breaker, 3)
        assert breaker.should_trip() is True
        return breaker

    def test_blocks_during_cooldown(self):
        breaker = self._opened_breaker()
        assert breaker.state == "open"
        assert breaker.should_trip() is True

    def test_half_open_probe_allowed_after_cooldown(self):
        breaker = self._opened_breaker()
        time.sleep(WAIT_S)
        assert breaker.state == "half-open"
        assert breaker.should_trip() is False

    def test_successful_probe_closes_breaker(self):
        breaker = self._opened_breaker()
        time.sleep(WAIT_S)
        assert breaker.should_trip() is False  # the single probe is dispatched
        breaker.record_success()  # probe succeeded
        assert breaker.should_trip() is False  # closed: normal calls flow again
        assert breaker.state == "closed"
        assert breaker._consecutive_failures == 0

    def test_failed_probe_reopens_for_new_cooldown(self):
        breaker = self._opened_breaker()
        time.sleep(WAIT_S)
        assert breaker.should_trip() is False  # probe dispatched
        breaker.record_failure()  # probe failed -> re-open
        assert breaker.should_trip() is True  # blocked again immediately
        assert breaker.state == "open"
        time.sleep(WAIT_S)
        assert breaker.should_trip() is False  # next probe window opens

    def test_only_single_probe_in_flight(self):
        breaker = self._opened_breaker()
        time.sleep(WAIT_S)
        assert breaker.should_trip() is False  # first caller takes the probe slot
        assert breaker.should_trip() is True  # a concurrent caller stays blocked

    def test_default_cooldown_is_about_30s(self):
        # Behavior contract from the issue ("cooldown ~30s"), not a snapshot:
        # the default must be tens of seconds — not zero (no recovery) and
        # not minutes (effectively still wedged for short sessions).
        default_cooldown = CircuitBreaker().cooldown_s
        assert 5.0 <= default_cooldown <= 120.0
        assert ter.BREAKER_DEFAULT_COOLDOWN_S == pytest.approx(30.0)

    def test_state_introspection(self):
        breaker = CircuitBreaker(threshold=2, cooldown_s=COOLDOWN_S)
        assert breaker.state == "closed"
        _trip(breaker, 2)
        assert breaker.state == "open"
        time.sleep(WAIT_S)
        assert breaker.state == "half-open"
        breaker.record_success()
        assert breaker.state == "closed"

    def test_seconds_until_probe_counts_down(self):
        breaker = CircuitBreaker(threshold=1, cooldown_s=10.0)
        breaker.record_failure()
        remaining = breaker.seconds_until_probe()
        assert 0.0 < remaining <= 10.0
        breaker.record_success()
        assert breaker.seconds_until_probe() == 0.0


class TestRegistryIntegration:
    """get_breaker / record_tool_outcome drive the same state machine."""

    def test_get_breaker_returns_same_instance(self):
        first = get_breaker("terminal_2439_test")
        second = get_breaker("terminal_2439_test")
        assert first is second

    def test_record_tool_outcome_opens_and_external_success_resets(self):
        name = "read_file_2439_test"
        for _ in range(5):
            record_tool_outcome(name, success=False)
        assert get_breaker(name).should_trip() is True  # open (30s default cooldown)
        record_tool_outcome(name, success=True)  # a success resets the breaker
        assert get_breaker(name).should_trip() is False

    def test_full_probe_cycle_through_record_tool_outcome(self):
        name = "patch_2439_test"
        breaker = get_breaker(name, cooldown_s=COOLDOWN_S)
        for _ in range(5):
            record_tool_outcome(name, success=False)
        assert breaker.should_trip() is True  # open
        time.sleep(WAIT_S)
        assert breaker.should_trip() is False  # half-open: probe allowed
        record_tool_outcome(name, success=True)  # probe succeeded
        assert breaker.should_trip() is False  # closed

    def test_failed_probe_through_record_tool_outcome_reopens(self):
        name = "search_files_2439_test"
        breaker = get_breaker(name, cooldown_s=COOLDOWN_S)
        for _ in range(5):
            record_tool_outcome(name, success=False)
        time.sleep(WAIT_S)
        assert breaker.should_trip() is False  # probe allowed
        record_tool_outcome(name, success=False)  # probe failed
        assert breaker.should_trip() is True  # re-opened for a fresh cooldown


class TestThreadSafety:
    def test_concurrent_callers_get_exactly_one_probe_slot(self):
        breaker = CircuitBreaker(threshold=2, cooldown_s=COOLDOWN_S)
        _trip(breaker, 2)
        time.sleep(WAIT_S)
        allowed: list[bool] = []
        start_gate = threading.Event()

        def contender():
            start_gate.wait()
            allowed.append(not breaker.should_trip())

        threads = [threading.Thread(target=contender) for _ in range(8)]
        for thread in threads:
            thread.start()
        start_gate.set()
        for thread in threads:
            thread.join()
        # Exactly one concurrent caller wins the probe slot; the rest stay
        # blocked until the probe outcome is recorded.
        assert allowed.count(True) == 1
