"""Tests for agent.tool_dedup — the When2Tool tool-call dedup tracker (#2282)."""

import pytest

from agent.tool_dedup import (
    DEFAULT_CONFIG,
    check_tool_dedup,
    record_tool_call,
    reset_tool_dedup,
    tool_dedup_enabled,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    """Reset the process-global registry between tests."""
    reset_tool_dedup(None)
    yield
    reset_tool_dedup(None)


# ── Config gate ──────────────────────────────────────────────────────────


def test_enabled_by_default(monkeypatch):
    """No env var and no config section -> ON (advisory, fail-open)."""
    monkeypatch.delenv("HERMES_TOOL_DEDUP", raising=False)
    assert tool_dedup_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "no", "off", "FALSE"])
def test_env_disables(monkeypatch, val):
    monkeypatch.setenv("HERMES_TOOL_DEDUP", val)
    assert tool_dedup_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "yes", "on"])
def test_env_enables(monkeypatch, val):
    monkeypatch.setenv("HERMES_TOOL_DEDUP", val)
    assert tool_dedup_enabled() is True


def test_config_disables_when_env_absent(monkeypatch):
    monkeypatch.delenv("HERMES_TOOL_DEDUP", raising=False)
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"tool_dedup": {"enabled": False}},
    )
    assert tool_dedup_enabled() is False


def test_config_load_error_defaults_on(monkeypatch):
    monkeypatch.delenv("HERMES_TOOL_DEDUP", raising=False)

    def _boom():
        raise RuntimeError("config unreadable")

    monkeypatch.setattr("hermes_cli.config.load_config", _boom)
    assert tool_dedup_enabled() is True


# ── Core dedup behavior ──────────────────────────────────────────────────


def test_no_hint_on_first_call():
    """A first call is never flagged as a duplicate."""
    assert check_tool_dedup("read_file", {"path": "/tmp/a.py"}) is None


def test_hint_on_immediate_repeat():
    """A repeat of a recently-successful call returns a hint."""
    record_tool_call("read_file", {"path": "/tmp/a.py"}, success=True)
    hint = check_tool_dedup("read_file", {"path": "/tmp/a.py"})
    assert hint is not None
    assert "read_file" in hint
    assert "Reuse that result" in hint


def test_no_hint_after_failed_call():
    """A failed call must not be treated as 'already done'."""
    record_tool_call("read_file", {"path": "/tmp/a.py"}, success=False)
    assert check_tool_dedup("read_file", {"path": "/tmp/a.py"}) is None


def test_no_hint_for_different_args():
    """Different arguments are not a duplicate."""
    record_tool_call("read_file", {"path": "/tmp/a.py"}, success=True)
    assert check_tool_dedup("read_file", {"path": "/tmp/b.py"}) is None


def test_no_hint_for_different_tool():
    """Different tool with same args is not a duplicate."""
    record_tool_call("read_file", {"path": "/tmp/a.py"}, success=True)
    assert check_tool_dedup("search_files", {"path": "/tmp/a.py"}) is None


def test_arg_order_insensitive():
    """Semantically-identical calls hash the same regardless of arg order."""
    record_tool_call("search_files", {"path": "/tmp", "pattern": "foo"}, success=True)
    hint = check_tool_dedup("search_files", {"pattern": "foo", "path": "/tmp"})
    assert hint is not None


def test_volatile_args_stripped():
    """Transport keys (task_id/session_id) do not break dedup identity."""
    record_tool_call(
        "read_file",
        {"path": "/tmp/a.py", "task_id": "t1", "session_id": "s1"},
        success=True,
    )
    hint = check_tool_dedup(
        "read_file",
        {"path": "/tmp/a.py", "task_id": "t2", "session_id": "s2"},
    )
    assert hint is not None


def test_session_isolation():
    """One session's calls never leak into another."""
    record_tool_call("read_file", {"path": "/tmp/a.py"}, session_id="sess-a", success=True)
    assert check_tool_dedup("read_file", {"path": "/tmp/a.py"}, session_id="sess-b") is None
    assert check_tool_dedup("read_file", {"path": "/tmp/a.py"}, session_id="sess-a") is not None


def test_untracked_tool_never_hints():
    """Tools outside the tracked set are never flagged."""
    record_tool_call("terminal", {"command": "ls"}, success=True)
    assert check_tool_dedup("terminal", {"command": "ls"}) is None


def test_recency_window_expires():
    """A repeat beyond the recency window is not flagged."""
    record_tool_call("read_file", {"path": "/tmp/a.py"}, success=True)
    # Advance the counter past the default window (20) with other calls.
    for _ in range(DEFAULT_CONFIG["recency_window"] + 1):
        record_tool_call("terminal", {"command": "echo x"}, success=True)
    assert check_tool_dedup("read_file", {"path": "/tmp/a.py"}) is None


def test_reset_clears_hints():
    """reset_tool_dedup clears state so a re-read is legitimate."""
    record_tool_call("read_file", {"path": "/tmp/a.py"}, success=True)
    assert check_tool_dedup("read_file", {"path": "/tmp/a.py"}) is not None
    reset_tool_dedup("default")
    assert check_tool_dedup("read_file", {"path": "/tmp/a.py"}) is None


def test_disabled_tracker_never_hints(monkeypatch):
    """When disabled, no hint is produced and nothing is recorded."""
    monkeypatch.setenv("HERMES_TOOL_DEDUP", "0")
    record_tool_call("read_file", {"path": "/tmp/a.py"}, success=True)
    assert check_tool_dedup("read_file", {"path": "/tmp/a.py"}) is None
