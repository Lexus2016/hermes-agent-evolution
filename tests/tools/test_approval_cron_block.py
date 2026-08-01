"""Tests for approval cron-context blocking (#1542, #1554).

Verifies that _is_unattended_context() and _cron_blocked_result() correctly
convert pending_approval into non-retryable blocked results in cron/subagent
contexts, preventing the retry spirals that cause 44% of terminal failures.

#1554: _is_unattended_context() now requires a *positive* signal
(HERMES_CRON_SESSION or HERMES_SUBAGENT) instead of defaulting to True
when nobody is "home" — the catch-all broke CI test environments.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from tools.approval import _cron_blocked_result, _is_unattended_context


class TestIsUnattendedContext:
    """_is_unattended_context() detects cron/subagent via positive signal."""

    def test_cron_session_is_unattended(self):
        """HERMES_CRON_SESSION=1 → always unattended."""
        with patch(
            "tools.approval.env_var_enabled",
            side_effect=lambda v: v == "HERMES_CRON_SESSION",
        ):
            assert _is_unattended_context() is True

    def test_subagent_is_unattended(self):
        """HERMES_SUBAGENT=1 → unattended (#1554 positive signal)."""
        with patch(
            "tools.approval.env_var_enabled",
            side_effect=lambda v: v == "HERMES_SUBAGENT",
        ):
            assert _is_unattended_context() is True

    def test_interactive_cli_not_unattended(self):
        """Interactive CLI with no cron → not unattended."""
        with patch("tools.approval.env_var_enabled", return_value=False):
            assert _is_unattended_context() is False

    def test_gateway_not_unattended(self):
        """Gateway session → not unattended."""
        with patch("tools.approval.env_var_enabled", return_value=False):
            assert _is_unattended_context() is False

    def test_no_context_not_unattended(self):
        """No positive signal → NOT unattended (#1554 fix).

        Previously defaulted to True (catch-all "nobody's home"), which broke
        CI test environments. Now requires HERMES_CRON_SESSION or
        HERMES_SUBAGENT.
        """
        with patch("tools.approval.env_var_enabled", return_value=False):
            assert _is_unattended_context() is False


class TestCronBlockedResult:
    """_cron_blocked_result() builds a non-retryable blocked response."""

    def test_returns_blocked_status(self):
        result = _cron_blocked_result("test desc", "rm -rf /")
        assert result["status"] == "blocked"
        assert result["approved"] is False
        assert result["approval_pending"] is False

    def test_message_says_do_not_retry(self):
        result = _cron_blocked_result("desc", "cmd")
        assert "Do NOT retry" in result["message"]

    def test_preserves_command_and_desc(self):
        result = _cron_blocked_result("my desc", "my cmd", pattern_key="pk")
        assert result["command"] == "my cmd"
        assert result["description"] == "my desc"
        assert result["pattern_key"] == "pk"
