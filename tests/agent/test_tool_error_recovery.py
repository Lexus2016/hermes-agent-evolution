"""Tests for agent.tool_error_recovery — tool-level error classification and circuit breaker."""

import pytest
from agent.tool_error_recovery import (
    CircuitBreaker,
    RecoveryAction,
    ToolErrorClass,
    ToolFailure,
    classify_tool_error,
    get_breaker,
    recovery_hint,
    record_tool_outcome,
)


# ── Classification tests ─────────────────────────────────────────────────


class TestClassifyToolError:
    """Pattern-based classification of tool error messages."""

    def test_not_found(self):
        result = classify_tool_error("read_file", "File not found: /tmp/foo.py")
        assert result.error_class == ToolErrorClass.not_found
        assert result.recovery_action == RecoveryAction.check_path
        assert "Verify the path exists" in result.hint

    def test_no_such_file(self):
        result = classify_tool_error("terminal", "[Errno 2] No such file or directory")
        assert result.error_class == ToolErrorClass.not_found

    def test_permission_denied(self):
        result = classify_tool_error("write_file", "Permission denied: /root/secret")
        assert result.error_class == ToolErrorClass.permission
        assert result.recovery_action == RecoveryAction.check_credentials

    def test_rate_limit(self):
        result = classify_tool_error("web_search", "Rate limit exceeded (429)")
        assert result.error_class == ToolErrorClass.rate_limit
        assert result.recovery_action == RecoveryAction.retry

    def test_timeout(self):
        result = classify_tool_error("terminal", "Command timed out after 30s")
        assert result.error_class == ToolErrorClass.transient
        assert result.recovery_action == RecoveryAction.retry

    def test_dependency_missing(self):
        result = classify_tool_error("terminal", "bash: rg: command not found")
        assert result.error_class == ToolErrorClass.dependency
        assert result.recovery_action == RecoveryAction.install_dependency

    def test_module_not_found(self):
        result = classify_tool_error("execute_code", "ModuleNotFoundError: No module named 'foo'")
        assert result.error_class == ToolErrorClass.dependency

    def test_validation_bad_args(self):
        result = classify_tool_error("patch", "Invalid arguments: expected str, got int")
        assert result.error_class == ToolErrorClass.validation
        assert result.recovery_action == RecoveryAction.fix_args

    def test_missing_required_param(self):
        result = classify_tool_error("terminal", "missing required argument: 'command'")
        assert result.error_class == ToolErrorClass.validation

    def test_json_parse_error(self):
        result = classify_tool_error("web_extract", "JSON decode error: unexpected token")
        assert result.error_class == ToolErrorClass.validation

    def test_unknown_error(self):
        result = classify_tool_error("custom_tool", "Something weird happened")
        assert result.error_class == ToolErrorClass.unknown
        assert result.recovery_action == RecoveryAction.escalate

    def test_empty_message(self):
        result = classify_tool_error("read_file", "")
        assert result.error_class == ToolErrorClass.unknown

    def test_none_message(self):
        result = classify_tool_error("read_file", str(None))
        assert result.error_class == ToolErrorClass.unknown

    def test_tool_name_preserved(self):
        result = classify_tool_error("my_tool", "File not found")
        assert result.tool_name == "my_tool"

    def test_attempt_number_preserved(self):
        result = classify_tool_error("terminal", "timeout", attempt=3)
        assert result.attempt_number == 3


# ── Recovery hint tests ──────────────────────────────────────────────────


class TestRecoveryHint:
    def test_hint_for_known_class(self):
        failure = ToolFailure(
            tool_name="read_file",
            error_message="File not found",
            error_class=ToolErrorClass.not_found,
            recovery_action=RecoveryAction.check_path,
            hint="Check the path.",
        )
        hint = recovery_hint(failure)
        assert "[check_path:" in hint
        assert "Check the path." in hint

    def test_no_hint_for_unknown(self):
        failure = ToolFailure(
            tool_name="custom",
            error_message="weird",
            error_class=ToolErrorClass.unknown,
            recovery_action=RecoveryAction.escalate,
            hint="",
        )
        assert recovery_hint(failure) == ""


# ── Circuit breaker tests ────────────────────────────────────────────────


class TestCircuitBreaker:
    def test_starts_closed(self):
        cb = CircuitBreaker(threshold=3)
        assert not cb.should_trip()

    def test_opens_after_threshold(self):
        cb = CircuitBreaker(threshold=3)
        cb.record_failure()
        assert not cb.should_trip()
        cb.record_failure()
        assert not cb.should_trip()
        cb.record_failure()
        assert cb.should_trip()

    def test_resets_on_success(self):
        cb = CircuitBreaker(threshold=2)
        cb.record_failure()
        cb.record_failure()
        assert cb.should_trip()
        cb.record_success()
        assert not cb.should_trip()

    def test_stays_open_on_more_failures(self):
        cb = CircuitBreaker(threshold=2)
        cb.record_failure()
        cb.record_failure()
        assert cb.should_trip()
        cb.record_failure()
        assert cb.should_trip()


class TestBreakerRegistry:
    def test_get_breaker_creates_new(self):
        breaker = get_breaker("test_tool_unique_1")
        assert breaker is not None
        assert not breaker.should_trip()

    def test_get_breaker_returns_same_instance(self):
        b1 = get_breaker("test_tool_unique_2")
        b2 = get_breaker("test_tool_unique_2")
        assert b1 is b2

    def test_record_outcome_success_resets(self):
        get_breaker("test_tool_unique_3")  # ensure exists
        record_tool_outcome("test_tool_unique_3", success=True)
        breaker = get_breaker("test_tool_unique_3")
        assert not breaker.should_trip()

    def test_record_outcome_failure_increments(self):
        breaker = get_breaker("test_tool_unique_4", threshold=10)
        for _ in range(5):
            record_tool_outcome("test_tool_unique_4", success=False)
        assert breaker._consecutive_failures == 5
        assert not breaker.should_trip()  # threshold is 10