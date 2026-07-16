"""Tests for tools.tool_failure_classifier.

The cross-tool classifier generalizes the terminal-only
``terminal_failure_classifier`` into a single entry point that classifies
failures from any core tool (terminal, file, search, browser, delegate, …)
into structured PALADIN-style categories (issue #1025, child of #1019).
"""

import pytest

import tools.tool_failure_classifier as tfc


# ---------------------------------------------------------------------------
# Tool-family detection
# ---------------------------------------------------------------------------


class TestToolFamily:
    @pytest.mark.parametrize(
        "tool_name, expected",
        [
            ("terminal", tfc.ToolType.terminal),
            ("terminal_tool", tfc.ToolType.terminal),
            ("read_file", tfc.ToolType.file),
            ("write_file", tfc.ToolType.file),
            ("apply_patch", tfc.ToolType.file),
            ("search_files", tfc.ToolType.search),
            ("web_search", tfc.ToolType.search),
            ("browser_tool", tfc.ToolType.browser),
            ("computer_use", tfc.ToolType.browser),
            ("delegate_task", tfc.ToolType.delegate),
            ("agent_team", tfc.ToolType.delegate),
        ],
    )
    def test_known_tools_map_to_family(self, tool_name, expected):
        assert tfc.tool_family(tool_name) == expected

    def test_unknown_tool_falls_back_to_generic(self):
        assert tfc.tool_family("some_unregistered_tool") == tfc.ToolType.generic

    def test_family_is_case_insensitive(self):
        assert tfc.tool_family("READ_FILE") == tfc.ToolType.file


# ---------------------------------------------------------------------------
# Generic (text-based) classification per category
# ---------------------------------------------------------------------------


class TestToolUnavailable:
    @pytest.mark.parametrize(
        "error",
        [
            "Yuanbao adapter is not connected",
            "openai package not installed",
            "WhatsApp plugin not registered or missing standalone_sender_fn",
            "STT is disabled in config.yaml (stt.enabled: false).",
            "Weixin adapter not available.",
        ],
    )
    def test_unavailable_dependencies(self, error):
        result = tfc.classify_tool_failure("some_tool", error)
        assert result.category == tfc.ToolFailureCategory.tool_unavailable
        assert result.should_retry is False


class TestInvalidArguments:
    @pytest.mark.parametrize(
        "error",
        [
            "path required",
            "old_string and new_string required",
            "old_text cannot be empty.",
            "Unknown mode: frobnicate",
            "Provide either 'goal' (single task) or 'tasks' (batch).",
            "Task 2 is missing a 'goal'.",
            "old_string not found. Use read_file to verify the current content.",
        ],
    )
    def test_argument_errors(self, error):
        result = tfc.classify_tool_failure("edit_file", error)
        assert result.category == tfc.ToolFailureCategory.invalid_arguments
        # A plain retry with the same arguments is futile.
        assert result.should_retry is False


class TestNotFound:
    @pytest.mark.parametrize(
        "error",
        [
            "File does not exist: /tmp/missing.txt",
            "No such file or directory",
            "No checkpoints exist for this directory",
        ],
    )
    def test_missing_target(self, error):
        result = tfc.classify_tool_failure("read_file", error)
        assert result.category == tfc.ToolFailureCategory.not_found
        assert result.should_retry is False


class TestPermissionDenied:
    @pytest.mark.parametrize(
        "error",
        [
            "Permission denied",
            "Operation not permitted",
            "Unauthorized RPC request",
            "Blocked: URL contains what appears to be an API key or token",
            "403 Forbidden",
        ],
    )
    def test_permission_errors(self, error):
        result = tfc.classify_tool_failure("browser_tool", error)
        assert result.category == tfc.ToolFailureCategory.permission_denied
        assert result.should_retry is False


class TestRateLimited:
    @pytest.mark.parametrize(
        "error",
        [
            "rate limit exceeded, retry after 30s",
            "429 Too Many Requests",
            "quota exceeded for this API key",
        ],
    )
    def test_rate_limit_errors(self, error):
        result = tfc.classify_tool_failure("web_search", error)
        assert result.category == tfc.ToolFailureCategory.rate_limited
        # Rate limits clear over time — a backoff retry can succeed.
        assert result.should_retry is True


class TestTransientNetwork:
    @pytest.mark.parametrize(
        "error",
        [
            "curl: (6) Could not resolve host: example.test",
            "Connection refused",
            "network is unreachable",
            "Connection reset by peer",
        ],
    )
    def test_network_errors(self, error):
        result = tfc.classify_tool_failure("web_search", error)
        assert result.category == tfc.ToolFailureCategory.transient_network
        assert result.should_retry is True


class TestTimeout:
    def test_timeout_text(self):
        result = tfc.classify_tool_failure(
            "browser_tool", "Browser command 'click' timed out after 30s"
        )
        assert result.category == tfc.ToolFailureCategory.timeout
        assert result.should_retry is True


class TestUnexpectedOutput:
    @pytest.mark.parametrize(
        "error",
        [
            "Provider returned a non-dict result",
            "Non-JSON output from agent-browser for 'snapshot': <html>",
            "Browser command 'eval' returned no output",
            "xAI STT returned empty transcript",
        ],
    )
    def test_malformed_output(self, error):
        result = tfc.classify_tool_failure("browser_tool", error)
        assert result.category == tfc.ToolFailureCategory.unexpected_output
        assert result.should_retry is True


class TestPersistentError:
    def test_generic_traceback_is_persistent(self):
        result = tfc.classify_tool_failure(
            "code_execution",
            "Traceback (most recent call last):\nRuntimeError: boom",
        )
        assert result.category == tfc.ToolFailureCategory.persistent_error
        assert result.should_retry is False


class TestUnknown:
    def test_empty_error_is_unknown(self):
        result = tfc.classify_tool_failure("some_tool", "")
        assert result.category == tfc.ToolFailureCategory.unknown
        assert result.should_retry is False


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------


class TestClassificationShape:
    def test_carries_tool_type_and_hint(self):
        result = tfc.classify_tool_failure("read_file", "path required")
        assert result.tool_type == tfc.ToolType.file
        assert isinstance(result.hint, str)
        assert result.hint  # non-empty, actionable

    def test_is_frozen(self):
        result = tfc.classify_tool_failure("read_file", "path required")
        with pytest.raises(Exception):
            result.category = tfc.ToolFailureCategory.unknown  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Terminal delegation: reuse the richer exit-code logic
# ---------------------------------------------------------------------------


class TestTerminalDelegation:
    def test_exit_124_is_timeout(self):
        result = tfc.classify_tool_failure("terminal", "", exit_code=124)
        assert result.category == tfc.ToolFailureCategory.timeout
        assert result.tool_type == tfc.ToolType.terminal
        assert result.should_retry is True

    def test_exit_127_is_tool_unavailable(self):
        result = tfc.classify_tool_failure(
            "terminal", "bash: nope: command not found", exit_code=127
        )
        assert result.category == tfc.ToolFailureCategory.tool_unavailable
        assert result.should_retry is False

    def test_exit_126_is_permission_denied(self):
        result = tfc.classify_tool_failure(
            "terminal", "bash: /root/x: Permission denied", exit_code=126
        )
        assert result.category == tfc.ToolFailureCategory.permission_denied
        assert result.should_retry is False

    def test_high_consecutive_count_downgrades_retry(self):
        result = tfc.classify_tool_failure(
            "terminal", "", exit_code=124, consecutive_count=5
        )
        # Repeated timeouts stop being retryable — force a strategy change.
        assert result.should_retry is False

    def test_deterministic_timeout_maps_to_persistent(self):
        """timeout_deterministic from terminal classifier maps to
        ToolFailureCategory.persistent_error (issue #1091)."""
        result = tfc.classify_tool_failure(
            "terminal", "", exit_code=124, consecutive_count=2
        )
        assert result.category == tfc.ToolFailureCategory.persistent_error
        assert result.should_retry is False
        assert result.tool_type == tfc.ToolType.terminal


# ---------------------------------------------------------------------------
# Extensibility
# ---------------------------------------------------------------------------


class TestRuleOrderFootguns:
    """Regression tests for first-match ordering hazards where a broad early
    pattern would otherwise steal an error that belongs to another category."""

    def test_deterministic_try_again_is_not_transient(self):
        # "try again" as friendly advice must not make a deterministic input
        # error look retryable.
        result = tfc.classify_tool_failure(
            "web_search", "invalid JSON. Try again with valid JSON."
        )
        assert result.category != tfc.ToolFailureCategory.transient_network

    def test_required_param_not_found_is_invalid_arguments(self):
        result = tfc.classify_tool_failure(
            "delegate_task", "Required parameter 'repo_name' not found."
        )
        assert result.category == tfc.ToolFailureCategory.invalid_arguments

    def test_missing_data_not_available_is_not_found(self):
        result = tfc.classify_tool_failure(
            "read_file", "The requested file version is not available."
        )
        assert result.category == tfc.ToolFailureCategory.not_found

    def test_adapter_not_available_is_still_tool_unavailable(self):
        result = tfc.classify_tool_failure("discord", "Weixin adapter not available.")
        assert result.category == tfc.ToolFailureCategory.tool_unavailable

    def test_blocked_transient_is_not_permission(self):
        result = tfc.classify_tool_failure(
            "browser_tool", "blocked: connection timed out while fetching package"
        )
        assert result.category == tfc.ToolFailureCategory.transient_network

    def test_blocked_security_is_still_permission(self):
        result = tfc.classify_tool_failure(
            "browser_tool",
            "Blocked: URL contains what appears to be an API key or token",
        )
        assert result.category == tfc.ToolFailureCategory.permission_denied

    def test_must_be_logged_in_is_permission(self):
        result = tfc.classify_tool_failure(
            "web_search", "You must be logged in to access this resource."
        )
        assert result.category == tfc.ToolFailureCategory.permission_denied


class TestExtensibility:
    def test_register_tool_family(self):
        tfc.register_tool_family("my_special_reader", tfc.ToolType.file)
        assert tfc.tool_family("my_special_reader") == tfc.ToolType.file

    def test_register_custom_rule(self):
        tfc.register_rule(
            r"kaboom sentinel pattern",
            tfc.ToolFailureCategory.rate_limited,
            should_retry=True,
        )
        result = tfc.classify_tool_failure("web_search", "kaboom sentinel pattern")
        assert result.category == tfc.ToolFailureCategory.rate_limited
        assert result.should_retry is True
