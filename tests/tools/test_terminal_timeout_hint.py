"""Tests for terminal timeout error enrichment (#1789, #3347).

The timeout error message includes a background-mode suggestion so the model
knows to re-run long commands with background=true instead of blind-retrying
in foreground mode.  Since #3347 it also warns that the process may still be
running or have produced partial output when a wall-clock timeout interrupts
it, so the agent checks for side effects before re-running.

These are real contract tests: they drive ``terminal_tool`` with an
environment double whose ``execute()`` raises a timeout and assert on the
tool's actual returned payload (not on a locally reconstructed string).
"""

import json

import pytest

from tools import terminal_tool


class TimingOutEnvironment:
    """Environment double whose execute() raises a timeout every call."""

    def __init__(self, message: str = "Command 'cmd' timed out after 120 seconds"):
        self._message = message
        self.calls = []
        self.env = {}
        self.cwd = ""

    def execute(self, command, **kwargs):
        self.calls.append((command, kwargs))
        raise TimeoutError(self._message)


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    for key in ("timeout-hint", "default"):
        terminal_tool._reset_terminal_streak(key)
        terminal_tool._reset_terminal_failure_repeats(key)
    yield
    for key in ("timeout-hint", "default"):
        terminal_tool._reset_terminal_streak(key)
        terminal_tool._reset_terminal_failure_repeats(key)


class TestTimeoutErrorEnrichment:
    """Verify the timeout error payload includes actionable guidance."""

    def test_timeout_error_mentions_background_and_partial_output(self, monkeypatch):
        """#1789 + #3347 — the enriched timeout error must tell the model to
        use background mode AND warn that the process may still be running or
        have produced partial output."""
        monkeypatch.setenv("TERMINAL_ENV", "local")
        fake = TimingOutEnvironment()
        monkeypatch.setattr(terminal_tool, "_active_environments", {"default": fake})

        data = json.loads(
            terminal_tool.terminal_tool("sleep 900", task_id="timeout-hint")
        )
        error = data["error"].lower()

        assert data["exit_code"] == 124
        assert "timed out" in error
        assert "background=true" in error
        assert "notify_on_complete=true" in error
        assert "re-run" in error
        # #3347 — partial-work warning (point a of the issue).
        assert "may still be running" in error
        assert "partial output" in error

    def test_timeout_error_includes_timeout_value(self, monkeypatch):
        """The timeout value should still be present in the enriched message."""
        monkeypatch.setenv("TERMINAL_ENV", "local")
        fake = TimingOutEnvironment()
        monkeypatch.setattr(terminal_tool, "_active_environments", {"default": fake})

        data = json.loads(
            terminal_tool.terminal_tool(
                "sleep 900", timeout=300, task_id="timeout-hint"
            )
        )
        assert "300 seconds" in data["error"]

    def test_timeout_is_not_marked_retryable(self, monkeypatch):
        """#1841 — timeouts are deterministic; the payload must not invite a
        blind foreground retry."""
        monkeypatch.setenv("TERMINAL_ENV", "local")
        fake = TimingOutEnvironment()
        monkeypatch.setattr(terminal_tool, "_active_environments", {"default": fake})

        data = json.loads(
            terminal_tool.terminal_tool("sleep 900", task_id="timeout-hint")
        )
        assert data["should_retry"] is False
