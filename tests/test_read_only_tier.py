"""Tests for the read-only tool tier (evo-2026-08-26-01).

HERMES-SUBAGENT-ATTRIBUTION subagent_id=sa-0-5295fac9 parent=root task_index=0 spawned_at=2026-08-26T00:06:28+00:00
"""

import os

import pytest

from agent.read_only_tier import (
    ENV_READ_ONLY,
    block_reason,
    read_only_mode_enabled,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(ENV_READ_ONLY, raising=False)


def test_disabled_by_default():
    # Enforcement gating lives in tool_executor (read_only_mode_enabled);
    # block_reason is a pure classifier and always classifies.
    assert read_only_mode_enabled(None) is False
    assert read_only_mode_enabled(object()) is False


def test_env_enables_mode():
    os.environ[ENV_READ_ONLY] = "1"
    assert read_only_mode_enabled(None) is True


def test_agent_attr_enables_mode():
    class A:
        read_only_mode = True

    assert read_only_mode_enabled(A()) is True


def test_read_only_tools_allowed():
    os.environ[ENV_READ_ONLY] = "1"
    assert block_reason("read_file", {"path": "/etc/hosts"}) is None
    assert block_reason("search_files", {"pattern": "x"}) is None


def test_mutating_terminal_blocked():
    os.environ[ENV_READ_ONLY] = "1"
    assert block_reason("terminal", {"command": "rm -rf /tmp/x"}) is not None
    assert block_reason("terminal", {"command": "git push origin main"}) is not None
    assert block_reason("shell", {"command": "echo hi > /etc/passwd"}) is not None


def test_observe_terminal_allowed():
    os.environ[ENV_READ_ONLY] = "1"
    assert block_reason("terminal", {"command": "ls -la"}) is None
    assert block_reason("terminal", {"command": "git status && git log -1"}) is None


def test_unknown_tool_fails_closed():
    os.environ[ENV_READ_ONLY] = "1"
    assert block_reason("deploy_to_prod", {}) is not None
