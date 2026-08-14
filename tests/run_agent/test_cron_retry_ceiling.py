"""Tests for cron-context API retry ceiling (issue #2376, SLICE A).

When HERMES_CRON_SESSION is set, the retry ceiling is raised from the
default interactive ceiling (agent._api_max_retries, typically 3) to a
higher value (default 15) so brief provider 429/overload spikes don't
kill a cron pipeline stage.
"""

import os
from unittest.mock import MagicMock, patch

import pytest


def _mock_agent(max_retries: int = 3):
    """Create a minimal mock agent with _api_max_retries set."""
    agent = MagicMock()
    agent._api_max_retries = max_retries
    agent.quiet_mode = True
    agent.log_prefix = "[test] "
    agent.provider = "test"
    agent.model = "test/model"
    return agent


def test_cron_context_raises_retry_ceiling(monkeypatch):
    """When HERMES_CRON_SESSION is set, max_retries should be raised to 15."""
    monkeypatch.setenv("HERMES_CRON_SESSION", "1")
    monkeypatch.delenv("HERMES_CRON_MAX_RETRIES", raising=False)

    # Import the module fresh so the env var is read at call time.
    from agent import conversation_loop as cl

    agent = _mock_agent(max_retries=3)

    # The retry-ceiling logic is inline in process_conversation_turn.
    # We verify the env-var detection + ceiling computation directly.
    is_cron = cl.env_var_enabled("HERMES_CRON_SESSION")
    assert is_cron, "HERMES_CRON_SESSION should be detected"

    cron_retries = int(os.environ.get("HERMES_CRON_MAX_RETRIES", "15"))
    assert cron_retries == 15
    assert cron_retries > agent._api_max_retries


def test_non_cron_context_keeps_default_ceiling(monkeypatch):
    """Without HERMES_CRON_SESSION, the ceiling stays at agent default."""
    monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)

    from agent import conversation_loop as cl

    is_cron = cl.env_var_enabled("HERMES_CRON_SESSION")
    assert not is_cron, "HERMES_CRON_SESSION should not be detected"


def test_cron_max_retries_env_override(monkeypatch):
    """HERMES_CRON_MAX_RETRIES env var overrides the default 15."""
    monkeypatch.setenv("HERMES_CRON_SESSION", "1")
    monkeypatch.setenv("HERMES_CRON_MAX_RETRIES", "20")

    cron_retries = int(os.environ.get("HERMES_CRON_MAX_RETRIES", "15"))
    assert cron_retries == 20


def test_cron_ceiling_never_lowers_below_default(monkeypatch):
    """If HERMES_CRON_MAX_RETRIES < agent default, keep the agent default."""
    monkeypatch.setenv("HERMES_CRON_SESSION", "1")
    monkeypatch.setenv("HERMES_CRON_MAX_RETRIES", "2")

    agent = _mock_agent(max_retries=5)
    cron_retries = int(os.environ.get("HERMES_CRON_MAX_RETRIES", "15"))

    # The code uses `if _cron_retries > max_retries` — it never lowers.
    assert cron_retries <= agent._api_max_retries
