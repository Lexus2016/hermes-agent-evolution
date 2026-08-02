"""Regression tests for terminal security-scan (tirith) blocks in cron/subagent
contexts (issue #1590).

When ``_check_all_guards`` returns a plain ``approved: False`` block (the shape
produced by the tirith security-scan cron-deny path — no ``status`` field, no
``pending_approval``), and no interactive user or gateway is present, the
terminal tool must still emit the #1542 non-retryable ``blocked`` treatment:
an explicit "Do NOT retry" directive and a steer toward approval-free
alternatives (write to a temp file, then read_file it).

Without this, the agent retries the same blocked command 2-3 times before
stalling — the cron-context failure mode described in #1590.
"""

import json
from unittest.mock import patch

import pytest

import tools.terminal_tool as terminal_tool


def _tirith_block_result():
    """The shape ``check_all_command_guards`` returns for a tirith security-scan
    block in cron-deny mode: ``approved: False`` with a message but NO
    ``status``/``pending_approval`` keys (unlike the gateway ask-mode path)."""
    return {
        "approved": False,
        "message": (
            "BLOCKED: pipe-to-interpreter pattern detected "
            "(command pipes output to python3) but cron jobs run without "
            "a user present to approve it. Find an alternative approach "
            "that avoids this command. To allow dangerous commands in "
            "cron jobs, set approvals.cron_mode: approve in config.yaml."
        ),
    }


@pytest.fixture
def _cron_env(monkeypatch):
    """Simulate a cron session — no interactive CLI, no gateway."""
    monkeypatch.setenv("HERMES_CRON_SESSION", "1")
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
    monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
    monkeypatch.setattr(
        terminal_tool, "_check_disk_usage_warning", lambda *a, **kw: None
    )
    yield


@pytest.fixture
def _subagent_env(monkeypatch):
    """Simulate a headless subagent — no cron, no CLI, no gateway."""
    monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
    monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
    monkeypatch.setattr(
        terminal_tool, "_check_disk_usage_warning", lambda *a, **kw: None
    )
    yield


class TestSecurityScanBlockCronConversion:
    """Verify a tirith security-scan block gets the #1542/#1590 treatment in
    cron/subagent contexts — even though it lacks the pending_approval status."""

    def test_cron_security_block_has_do_not_retry(self, _cron_env):
        """A tirith block in cron context must carry 'Do NOT retry' so the
        agent stops retrying the same blocked command."""
        with patch.object(
            terminal_tool, "_check_all_guards", return_value=_tirith_block_result()
        ):
            result = terminal_tool.terminal_tool(
                command="gh pr list | python3 -c 'import sys; print(sys.stdin.read())'",
            )

        parsed = json.loads(result)
        assert parsed["status"] == "blocked", (
            f"Expected status='blocked', got {parsed.get('status')!r}"
        )
        assert parsed["exit_code"] == -1
        error = parsed.get("error", "")
        assert "Do NOT retry" in error, (
            "Security-scan block in cron context must contain 'Do NOT retry' "
            f"directive; got error={error!r}"
        )
        assert "cron" in error.lower()

    def test_cron_security_block_steers_to_alternatives(self, _cron_env):
        """The block must steer toward approval-free alternatives (temp file +
        read_file, or file/search tools) — not just say 'find an alternative'."""
        with patch.object(
            terminal_tool, "_check_all_guards", return_value=_tirith_block_result()
        ):
            result = terminal_tool.terminal_tool(
                command="gh pr list | python3 -c 'import sys'",
            )

        parsed = json.loads(result)
        error = parsed.get("error", "").lower()
        assert "read_file" in error or "temp file" in error, (
            "Block should steer toward writing output to a temp file + read_file"
        )

    def test_subagent_security_block_also_treated(self, _subagent_env):
        """A headless subagent (no cron, no CLI, no gateway) must ALSO get the
        blocked treatment for a security-scan block."""
        with patch.object(
            terminal_tool, "_check_all_guards", return_value=_tirith_block_result()
        ):
            result = terminal_tool.terminal_tool(
                command="cat data.json | python3 -m json.tool",
            )

        parsed = json.loads(result)
        assert parsed["status"] == "blocked"
        assert "Do NOT retry" in parsed.get("error", "")
        assert "subagent" in parsed.get("error", "").lower()

    def test_interactive_block_unchanged(self, monkeypatch):
        """In an interactive CLI session, the block message must NOT get the
        cron/subagent 'Do NOT retry' addendum — the user can still approve."""
        monkeypatch.setenv("HERMES_INTERACTIVE", "1")
        monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)
        monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
        monkeypatch.setattr(
            terminal_tool, "_check_disk_usage_warning", lambda *a, **kw: None
        )
        raw = _tirith_block_result()
        with patch.object(terminal_tool, "_check_all_guards", return_value=raw):
            result = terminal_tool.terminal_tool(
                command="gh pr list | python3 -c 'import sys'",
            )

        parsed = json.loads(result)
        # Original message preserved; no cron-style addendum appended.
        assert parsed["status"] == "blocked"
        assert parsed["error"] == raw["message"]
        assert "Do NOT retry" not in parsed["error"]

    def test_existing_do_not_retry_not_duplicated(self, _cron_env):
        """If the block message already contains 'Do NOT retry' (e.g. a hardline
        or deny-rule block), the addendum must not be appended a second time."""
        raw = _tirith_block_result()
        raw["message"] = "BLOCKED: hardline. Do NOT retry this command."
        with patch.object(terminal_tool, "_check_all_guards", return_value=raw):
            result = terminal_tool.terminal_tool(
                command="rm -rf /",
            )

        parsed = json.loads(result)
        assert parsed["error"].count("Do NOT retry") == 1, (
            "'Do NOT retry' must not be duplicated when already present"
        )
