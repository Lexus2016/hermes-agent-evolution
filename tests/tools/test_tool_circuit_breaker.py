# -*- coding: utf-8 -*-
"""Unit tests for tools.tool_circuit_breaker (#3241)."""

import pytest
from tools.tool_circuit_breaker import (
    _ToolCircuitBreakerTracker,
    classify_tool_error,
    get_strategy_recommendation,
    FAILURE_CLASS_NOT_FOUND,
    FAILURE_CLASS_PERMISSION,
    FAILURE_CLASS_INVALID_ARG,
    FAILURE_CLASS_TIMEOUT,
    FAILURE_CLASS_PROVIDER,
    FAILURE_CLASS_TRANSIENT,
)


class TestClassifyToolError:
    def test_not_found(self):
        assert classify_tool_error("FileNotFoundError: [Errno 2] No such file or directory: 'foo.txt'") == FAILURE_CLASS_NOT_FOUND
        assert classify_tool_error("Cannot find path /bar") == FAILURE_CLASS_NOT_FOUND

    def test_permission_denied(self):
        assert classify_tool_error("PermissionError: [Errno 13] Permission denied") == FAILURE_CLASS_PERMISSION

    def test_invalid_arg(self):
        assert classify_tool_error("ValueError: invalid literal for int()") == FAILURE_CLASS_INVALID_ARG

    def test_timeout(self):
        assert classify_tool_error("TimeoutError: command timed out after 30s") == FAILURE_CLASS_TIMEOUT

    def test_provider_error(self):
        assert classify_tool_error("API rate limit exceeded (429)") == FAILURE_CLASS_PROVIDER

    def test_transient_fallback(self):
        assert classify_tool_error("Unexpected unknown anomaly") == FAILURE_CLASS_TRANSIENT
        assert classify_tool_error(None) == FAILURE_CLASS_TRANSIENT


class TestStrategyRecommendation:
    def test_patch_recommendation(self):
        rec = get_strategy_recommendation("patch", FAILURE_CLASS_NOT_FOUND)
        assert "read_file" in rec

    def test_read_file_recommendation(self):
        rec = get_strategy_recommendation("read_file", FAILURE_CLASS_NOT_FOUND)
        assert "target path exists" in rec

    def test_terminal_recommendation(self):
        rec = get_strategy_recommendation("terminal", FAILURE_CLASS_TIMEOUT)
        assert "background task" in rec or "prerequisites" in rec


class TestToolCircuitBreakerTracker:
    def test_single_failure_does_not_trip(self):
        tracker = _ToolCircuitBreakerTracker()
        event = tracker.record_result("sess1", "read_file", "error", "file not found", budget=3)
        assert event is None

    def test_exhausted_budget_trips_circuit_breaker(self):
        tracker = _ToolCircuitBreakerTracker()
        tracker.record_result("sess1", "read_file", "error", "file not found", budget=3)
        tracker.record_result("sess1", "read_file", "error", "file not found", budget=3)
        event = tracker.record_result("sess1", "read_file", "error", "file not found", budget=3)
        assert event is not None
        assert event["circuit_breaker_tripped"] is True
        assert event["consecutive_failures"] == 3
        assert event["failure_class"] == FAILURE_CLASS_NOT_FOUND
        assert "target path exists" in event["strategy_recommendation"]

    def test_success_resets_consecutive_failures(self):
        tracker = _ToolCircuitBreakerTracker()
        tracker.record_result("sess1", "patch", "error", "patch reject", budget=3)
        tracker.record_result("sess1", "patch", "error", "patch reject", budget=3)
        # Success resets
        tracker.record_result("sess1", "patch", "ok")
        event = tracker.record_result("sess1", "patch", "error", "patch reject", budget=3)
        assert event is None  # Only 1 consecutive failure now

    def test_session_isolation(self):
        tracker = _ToolCircuitBreakerTracker()
        for _ in range(3):
            tracker.record_result("sess1", "terminal", "error", "command failed", budget=3)
        stats1 = tracker.get_session_stats("sess1")
        stats2 = tracker.get_session_stats("sess2")
        assert stats1["terminal"]["consecutive_failures"] == 3
        assert "terminal" not in stats2
