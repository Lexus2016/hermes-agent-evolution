"""Tests for approval cron-context blocking (#1542).

Verifies that _is_unattended_context() and _cron_blocked_result() correctly
convert pending_approval into non-retryable blocked results in cron/subagent
contexts, preventing the retry spirals that cause 44% of terminal failures.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from tools.approval import (
    _cron_blocked_result,
    _is_unattended_context,
    _is_subagent_context,
    set_hermes_subagent_context,
)


class TestIsUnattendedContext:
    """_is_unattended_context() requires a positive cron/subagent signal.

    Per #1554, the function must NOT default to "nobody's home" when none of
    interactive-CLI / gateway / cron / subagent are detected — that catch-all
    misfires in the non-interactive test environment, breaking the suite.
    """

    def test_cron_session_is_unattended(self):
        """HERMES_CRON_SESSION=1 → always unattended."""
        with patch(
            "tools.approval.env_var_enabled",
            side_effect=lambda v: v == "HERMES_CRON_SESSION",
        ):
            with patch("tools.approval._is_subagent_context", return_value=False):
                assert _is_unattended_context() is True

    def test_interactive_cli_not_unattended(self):
        """Interactive CLI with no cron/subagent → not unattended."""
        with patch("tools.approval.env_var_enabled", return_value=False):
            with patch("tools.approval._is_interactive_cli", return_value=True):
                with patch(
                    "tools.approval._is_gateway_approval_context", return_value=False
                ):
                    with patch(
                        "tools.approval._is_subagent_context", return_value=False
                    ):
                        assert _is_unattended_context() is False

    def test_gateway_not_unattended(self):
        """Gateway session → not unattended."""
        with patch("tools.approval.env_var_enabled", return_value=False):
            with patch("tools.approval._is_interactive_cli", return_value=False):
                with patch(
                    "tools.approval._is_gateway_approval_context", return_value=True
                ):
                    with patch(
                        "tools.approval._is_subagent_context", return_value=False
                    ):
                        assert _is_unattended_context() is False

    def test_no_context_is_not_unattended(self):
        """No interactive CLI, no gateway, no cron, no subagent → NOT unattended.

        This is the #1554 fix: the prior catch-all "nobody's home → unattended"
        default misfired in the non-interactive test environment (and any
        bare-script context), turning pending_approval into blocked and
        breaking tests. Absent a positive cron/subagent signal, we return
        False so callers fall back to pending_approval.
        """
        with patch("tools.approval.env_var_enabled", return_value=False):
            with patch("tools.approval._is_interactive_cli", return_value=False):
                with patch(
                    "tools.approval._is_gateway_approval_context", return_value=False
                ):
                    with patch(
                        "tools.approval._is_subagent_context", return_value=False
                    ):
                        assert _is_unattended_context() is False

    def test_subagent_context_is_unattended(self):
        """Subagent contextvar set → unattended (the real subagent path)."""
        with patch("tools.approval.env_var_enabled", return_value=False):
            with patch("tools.approval._is_subagent_context", return_value=True):
                assert _is_unattended_context() is True


class TestIsSubagentContext:
    """_is_subagent_context() reads the contextvar, then the env var."""

    def test_default_not_subagent(self):
        """No contextvar / env var set → not a subagent."""
        with patch("tools.approval._hermes_subagent_ctx") as ctx_mock:
            ctx_mock.get.return_value = None
            with patch("tools.approval.env_var_enabled", return_value=False):
                assert _is_subagent_context() is False

    def test_contextvar_set_is_subagent(self):
        """Contextvar set to '1' by _run_single_child → subagent."""
        token = set_hermes_subagent_context(True)
        try:
            assert _is_subagent_context() is True
        finally:
            from tools.approval import _hermes_subagent_ctx

            _hermes_subagent_ctx.reset(token)

    def test_contextvar_reset_clears_subagent(self):
        """Resetting the token restores the prior (non-subagent) state."""
        token = set_hermes_subagent_context(True)
        from tools.approval import _hermes_subagent_ctx

        _hermes_subagent_ctx.reset(token)
        with patch("tools.approval.env_var_enabled", return_value=False):
            assert _is_subagent_context() is False


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
