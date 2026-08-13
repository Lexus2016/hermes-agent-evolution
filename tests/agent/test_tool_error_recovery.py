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
        # #2168 — permission errors now steer toward alternatives instead of
        # just "check credentials" (the agent can't elevate credentials).
        assert result.recovery_action == RecoveryAction.use_alternative

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
        result = classify_tool_error(
            "execute_code", "ModuleNotFoundError: No module named 'foo'"
        )
        assert result.error_class == ToolErrorClass.dependency

    def test_validation_bad_args(self):
        result = classify_tool_error(
            "patch", "Invalid arguments: expected str, got int"
        )
        assert result.error_class == ToolErrorClass.validation
        assert result.recovery_action == RecoveryAction.fix_args

    def test_missing_required_param(self):
        result = classify_tool_error("terminal", "missing required argument: 'command'")
        assert result.error_class == ToolErrorClass.validation

    def test_json_parse_error(self):
        result = classify_tool_error(
            "web_extract", "JSON decode error: unexpected token"
        )
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


class TestClassifyToolException:
    """Tests for classify_tool_exception — exception-type-aware classification (#2245)."""

    def test_timeout_is_transient_retry(self):
        import asyncio
        from agent.tool_error_recovery import (
            classify_tool_exception,
            ToolErrorClass,
            RecoveryAction,
        )

        f = classify_tool_exception("tool_call", asyncio.TimeoutError())
        assert f.error_class == ToolErrorClass.transient
        assert f.recovery_action == RecoveryAction.retry

    def test_connection_error_is_transient(self):
        from agent.tool_error_recovery import (
            classify_tool_exception,
            ToolErrorClass,
            RecoveryAction,
        )

        f = classify_tool_exception("tool_call", ConnectionError("refused"))
        assert f.error_class == ToolErrorClass.transient
        assert f.recovery_action == RecoveryAction.retry

    def test_value_error_is_validation(self):
        from agent.tool_error_recovery import (
            classify_tool_exception,
            ToolErrorClass,
            RecoveryAction,
        )

        f = classify_tool_exception("tool_call", ValueError("bad arg"))
        assert f.error_class == ToolErrorClass.validation
        assert f.recovery_action == RecoveryAction.fix_args

    def test_key_error_is_not_found(self):
        from agent.tool_error_recovery import (
            classify_tool_exception,
            ToolErrorClass,
            RecoveryAction,
        )

        f = classify_tool_exception("tool_call", KeyError("missing_tool"))
        assert f.error_class == ToolErrorClass.not_found
        assert f.recovery_action == RecoveryAction.use_alternative

    def test_http_404_is_not_found(self):
        from agent.tool_error_recovery import (
            classify_tool_exception,
            ToolErrorClass,
            RecoveryAction,
        )

        class FakeHTTPError(Exception):
            status_code = 404

        f = classify_tool_exception("tool_call", FakeHTTPError("not found"))
        assert f.error_class == ToolErrorClass.not_found

    def test_http_500_is_transient_alternative(self):
        from agent.tool_error_recovery import (
            classify_tool_exception,
            ToolErrorClass,
            RecoveryAction,
        )

        class FakeHTTPError(Exception):
            status_code = 503

        f = classify_tool_exception("tool_call", FakeHTTPError("unavailable"))
        assert f.error_class == ToolErrorClass.transient
        assert f.recovery_action == RecoveryAction.use_alternative

    def test_http_400_is_validation(self):
        from agent.tool_error_recovery import (
            classify_tool_exception,
            ToolErrorClass,
            RecoveryAction,
        )

        class FakeHTTPError(Exception):
            status = 400

        f = classify_tool_exception("tool_call", FakeHTTPError("bad request"))
        assert f.error_class == ToolErrorClass.validation

    def test_generic_exception_falls_back_to_string(self):
        """An unrecognised exception type falls back to string classification."""
        from agent.tool_error_recovery import classify_tool_exception, ToolFailure

        f = classify_tool_exception("tool_call", RuntimeError("file not found in path"))
        assert isinstance(f, ToolFailure)
        # Should have been classified by the string "file not found" pattern.
        assert "not_found" in f.error_class.value or "unknown" in f.error_class.value

    def test_hint_is_nonempty_for_classified(self):
        from agent.tool_error_recovery import classify_tool_exception

        f = classify_tool_exception("tool_call", TimeoutError())
        assert f.hint != ""


class TestClassifyMcpError:
    """Tests for MCP / JSON-RPC structural classification (#2336)."""

    class _FakeMcpError(Exception):
        """Mimics mcp.shared.exceptions.McpError: .error.code + .error.message."""

        def __init__(self, code, message):
            super().__init__(f"McpError: {message}")
            er = type("E", (), {"code": code, "message": message})()
            self.error = er

    @pytest.mark.parametrize(
        "code,message,exp_cls,exp_action",
        [
            (-32601, "Method not found", "not_found", "use_alternative"),
            (-32602, "Invalid params", "validation", "fix_args"),
            (-32603, "Internal error", "transient", "retry"),
            (-32050, "Server disconnected", "transient", "retry"),
            (-32700, "Parse error", "validation", "fix_args"),
            (-99999, "Weird error", "unknown", "use_alternative"),
        ],
    )
    def test_mcp_code_classification(self, code, message, exp_cls, exp_action):
        from agent.tool_error_recovery import (
            classify_tool_exception,
            ToolErrorClass,
            RecoveryAction,
        )

        f = classify_tool_exception("tool_call", self._FakeMcpError(code, message))
        assert f.error_class == ToolErrorClass(exp_cls)
        assert f.recovery_action == RecoveryAction(exp_action)
        assert f.hint != ""

    def test_non_mcp_exception_falls_through(self):
        """A plain RuntimeError must reach the string classifier, not MCP path."""
        from agent.tool_error_recovery import classify_tool_exception, ToolFailure

        f = classify_tool_exception("tool_call", RuntimeError("file not found"))
        assert isinstance(f, ToolFailure)
        assert "not_found" in f.error_class.value or "unknown" in f.error_class.value
