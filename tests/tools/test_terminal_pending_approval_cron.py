"""Regression tests for terminal pending_approval → blocked conversion in
cron/subagent contexts (issue #1542).

When ``_check_all_guards`` returns ``status: pending_approval`` and no
interactive user or gateway is present (cron session, headless subagent),
the terminal tool must convert the result to a non-retryable ``blocked``
status with an explicit "Do NOT retry" directive.  Returning a bare
``pending_approval`` with an empty error string causes the agent to retry
the same blocked command 3-4 times — the #1 terminal failure reason
(14/32 = 44 % of terminal failures).
"""

import json
from unittest.mock import patch

import pytest

import tools.terminal_tool as terminal_tool


def _pending_approval_result():
    """The exact shape ``_check_all_guards`` returns for an approval gate."""
    return {
        "approved": False,
        "status": "pending_approval",
        "approval_pending": True,
        "command": "rm -rf /tmp/test",
        "description": "destructive command flagged",
        "pattern_key": "delete_in_root_path",
        "smart_denied": False,
        "allow_permanent": True,
    }


@pytest.fixture
def _cron_env(monkeypatch):
    """Simulate a cron session — no interactive CLI, no gateway."""
    monkeypatch.setenv("HERMES_CRON_SESSION", "1")
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
    monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
    # Suppress disk-usage side effects.
    monkeypatch.setattr(
        terminal_tool, "_check_disk_usage_warning", lambda *a, **kw: None
    )
    yield


class TestPendingApprovalCronConversion:
    """Verify pending_approval is converted to non-retryable blocked in cron."""

    def test_cron_session_gets_blocked_not_pending(self, monkeypatch, _cron_env):
        """In a cron session, pending_approval must become a blocked result
        with a clear 'Do NOT retry' directive — not a bare pending_approval."""
        with patch.object(
            terminal_tool, "_check_all_guards", return_value=_pending_approval_result()
        ):
            result = terminal_tool.terminal_tool(
                command="rm -rf /tmp/test",
            )

        parsed = json.loads(result)

        # MUST be non-retryable — status blocked, not pending_approval.
        assert parsed["status"] == "blocked", (
            "Cron pending_approval must convert to 'blocked', "
            f"got status={parsed.get('status')!r}"
        )
        assert parsed.get("approval_pending") is False, (
            "approval_pending must be False after conversion to blocked"
        )

        # Error must be non-empty and contain the SWITCH STRATEGY directive.
        error = parsed.get("error", "")
        assert error, (
            "Error string must be non-empty so the agent does not retry blindly"
        )
        assert "Do NOT retry" in error, (
            "Error must contain the explicit 'Do NOT retry' directive"
        )
        assert "BLOCKED" in error, "Error must signal a hard block, not a soft prompt"

    def test_blocked_result_preserves_pattern_metadata(self, monkeypatch, _cron_env):
        """The blocked result must preserve pattern_key/description so the
        caller can understand WHY the command was flagged."""
        raw = _pending_approval_result()
        with patch.object(terminal_tool, "_check_all_guards", return_value=raw):
            result = terminal_tool.terminal_tool(
                command="rm -rf /tmp/test",
            )

        parsed = json.loads(result)
        assert parsed["pattern_key"] == "delete_in_root_path"
        assert parsed["description"] == "destructive command flagged"
        assert parsed["exit_code"] == -1

    def test_non_cron_non_interactive_also_blocks(self, monkeypatch):
        """A headless subagent (no cron, no CLI, no gateway) must ALSO get
        the blocked conversion — not just cron sessions."""
        monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)
        monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
        monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
        monkeypatch.setattr(
            terminal_tool, "_check_disk_usage_warning", lambda *a, **kw: None
        )

        with patch.object(
            terminal_tool, "_check_all_guards", return_value=_pending_approval_result()
        ):
            result = terminal_tool.terminal_tool(
                command="rm -rf /tmp/test",
            )

        parsed = json.loads(result)
        assert parsed["status"] == "blocked"
        assert "Do NOT retry" in parsed.get("error", "")

    def test_cron_block_message_mentions_alternative_tools(
        self, monkeypatch, _cron_env
    ):
        """The directive should steer the agent toward file/search tools
        instead of more terminal retries."""
        with patch.object(
            terminal_tool, "_check_all_guards", return_value=_pending_approval_result()
        ):
            result = terminal_tool.terminal_tool(
                command="rm -rf /tmp/test",
            )

        parsed = json.loads(result)
        error = parsed.get("error", "")
        assert "alternative" in error.lower() or "file/search" in error.lower(), (
            "Error should suggest using file/search tools as an alternative"
        )
