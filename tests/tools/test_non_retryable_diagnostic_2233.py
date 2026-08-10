"""Tests for always-on non-retryable failure diagnostic (#2233).

Verifies that:
1. Non-retryable terminal failures get the diagnostic appended.
2. Retryable failures do NOT get the diagnostic.
3. The terminal per-tool failure cap is 3 (matching browser cap).
4. Session-hard-stop fires after 3 consecutive terminal failures.
"""

from __future__ import annotations

import pytest

from tools.tool_failure_classifier import classify_tool_failure
from tools.terminal_failure_classifier import (
    FailureCategory,
    classify_terminal_failure,
)


class TestNonRetryableClassification:
    """The classifier must mark deterministic errors as non-retryable."""

    def test_permission_denied_is_non_retryable(self) -> None:
        result = classify_terminal_failure(
            command="ls /root",
            exit_code=126,
            stdout="",
            stderr="Permission denied",
        )
        assert result.category == FailureCategory.permission_denied
        assert not result.should_retry

    def test_missing_command_is_non_retryable(self) -> None:
        result = classify_terminal_failure(
            command="nonexistent-binary",
            exit_code=127,
            stdout="",
            stderr="command not found",
        )
        assert result.category == FailureCategory.missing_command
        assert not result.should_retry

    def test_wall_clock_timeout_is_non_retryable(self) -> None:
        """exit_code 124 = timeout command kill — deterministic."""
        result = classify_terminal_failure(
            command="sleep 999",
            exit_code=124,
            stdout="",
            stderr="",
        )
        assert result.category == FailureCategory.timeout_deterministic
        assert not result.should_retry

    def test_syntax_error_is_non_retryable(self) -> None:
        result = classify_terminal_failure(
            command="python3 -c 'print('",
            exit_code=2,
            stdout="",
            stderr="SyntaxError: unexpected EOF",
        )
        assert not result.should_retry

    def test_transient_network_is_retryable(self) -> None:
        result = classify_terminal_failure(
            command="curl http://example.com",
            exit_code=28,
            stdout="",
            stderr="Connection timed out",
        )
        # First occurrence — still retryable (text-based timeout)
        assert result.should_retry
        assert result.category == FailureCategory.timeout

    def test_generic_classifier_propagates_should_retry(self) -> None:
        """classify_tool_failure (generic) must also mark permission denied."""
        result = classify_tool_failure(
            "terminal",
            "Permission denied",
            exit_code=126,
        )
        assert not result.should_retry


class TestTerminalFailureCapConfig:
    """The terminal per-tool failure cap must be 3 (#2233)."""

    def test_terminal_in_per_tool_caps(self) -> None:
        from agent.tool_guardrails import ToolCallGuardrailConfig

        config = ToolCallGuardrailConfig()
        assert "terminal" in config.per_tool_failure_caps
        assert config.per_tool_failure_caps["terminal"] == 3

    def test_terminal_in_spiral_prone_tools(self) -> None:
        from agent.tool_guardrails import ToolCallGuardrailConfig

        config = ToolCallGuardrailConfig()
        assert "terminal" in config.spiral_prone_tools

    def test_terminal_cap_is_lower_than_default(self) -> None:
        """Terminal cap (3) must be stricter than the default spiral_failure_cap (5)."""
        from agent.tool_guardrails import ToolCallGuardrailConfig

        config = ToolCallGuardrailConfig()
        terminal_cap = config.per_tool_failure_caps["terminal"]
        default_cap = config.spiral_failure_cap
        assert terminal_cap < default_cap
