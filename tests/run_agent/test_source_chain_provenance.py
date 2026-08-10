"""Tests for source-chain provenance lifecycle in background-review forks (#2192).

The source chain must be initialized at background-review fork start and
reset at fork end, so tool calls during the fork record their provenance.
"""

from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest

import run_agent as run_agent_module
from run_agent import AIAgent


def _bare_agent() -> AIAgent:
    agent = object.__new__(AIAgent)
    agent.model = "fake-model"
    agent.platform = "telegram"
    agent.provider = "openai"
    agent.base_url = ""
    agent.api_key = ""
    agent.api_mode = ""
    agent.session_id = "test-session"
    agent._parent_session_id = ""
    agent._credential_pool = None
    agent._memory_store = object()
    agent._memory_enabled = True
    agent._user_profile_enabled = False
    agent._cached_system_prompt = "test-cached-system-prompt"
    import datetime as _dt
    agent.session_start = _dt.datetime(2026, 1, 1, 12, 0, 0)
    agent._MEMORY_REVIEW_PROMPT = "review memory"
    agent._SKILL_REVIEW_PROMPT = "review skills"
    agent._COMBINED_REVIEW_PROMPT = "review both"
    agent.background_review_callback = None
    agent.status_callback = None
    agent._safe_print = lambda *_args, **_kwargs: None
    agent.reasoning_config = None
    agent.ephemeral_system_prompt = None
    agent.prefill_messages = None
    agent.enabled_toolsets = None
    agent.disabled_toolsets = None
    agent.memory_notifications = "on"
    agent._skip_mcp_refresh = True
    return agent


class ImmediateThread:
    def __init__(self, *, target, daemon=None, name=None):
        self._target = target

    def start(self):
        self._target()


class TestSourceChainProvenanceLifecycle:
    """Verify init_source_chain / reset_source_chain are called during
    background-review fork execution (#2192)."""

    def test_init_and_reset_source_chain_called(self, monkeypatch):
        """When a background-review fork runs, init_source_chain() is called
        before the conversation and reset_source_chain() is called after."""

        init_calls = []
        reset_calls = []

        # Track init/reset calls
        from tools import skill_provenance

        real_init = skill_provenance.init_source_chain
        real_reset = skill_provenance.reset_source_chain

        def tracking_init():
            init_calls.append(True)
            return real_init()

        def tracking_reset(token):
            reset_calls.append(token)
            real_reset(token)

        monkeypatch.setattr(skill_provenance, "init_source_chain", tracking_init)
        monkeypatch.setattr(skill_provenance, "reset_source_chain", tracking_reset)

        # Also patch at the import site in background_review (the module
        # imports locally so this patch must be visible at call time).
        from agent import background_review as _br

        monkeypatch.setattr(
            "tools.skill_provenance.init_source_chain", tracking_init
        )
        monkeypatch.setattr(
            "tools.skill_provenance.reset_source_chain", tracking_reset
        )

        class FakeReviewAgent:
            def __init__(self, **kwargs):
                self._session_messages = []
                self._memory_write_origin = ""
                self._memory_write_context = ""

            def run_conversation(self, **kwargs):
                # Simulate a tool call that adds provenance during the fork.
                from tools.skill_provenance import add_provenance_entry
                add_provenance_entry("terminal", source_id="/some/path")

            def shutdown_memory_provider(self):
                pass

            def close(self):
                pass

        monkeypatch.setattr(run_agent_module, "AIAgent", FakeReviewAgent)
        monkeypatch.setattr(run_agent_module.threading, "Thread", ImmediateThread)

        agent = _bare_agent()

        AIAgent._spawn_background_review(
            agent,
            messages_snapshot=[{"role": "user", "content": "hello"}],
            review_memory=True,
        )

        assert len(init_calls) == 1, "init_source_chain must be called once"
        assert len(reset_calls) == 1, "reset_source_chain must be called once"

    def test_source_chain_populated_during_fork(self, monkeypatch):
        """add_provenance_entry during a background-review fork actually
        records entries when init_source_chain has been called."""

        from tools.skill_provenance import (
            init_source_chain,
            reset_source_chain,
            add_provenance_entry,
            get_recorded_chain,
            set_current_write_origin,
            reset_current_write_origin,
            BACKGROUND_REVIEW,
        )

        token_origin = set_current_write_origin(BACKGROUND_REVIEW)
        chain_token = init_source_chain()
        try:
            add_provenance_entry("terminal", source_id="/path/a")
            add_provenance_entry("web_extract", source_id="https://example.com")
            chain = get_recorded_chain()
            assert len(chain) == 2
            assert chain[0]["source_type"] == "terminal"
            assert chain[0]["trusted"] is True
            assert chain[1]["source_type"] == "web_extract"
            assert chain[1]["trusted"] is False
        finally:
            reset_source_chain(chain_token)
            reset_current_write_origin(token_origin)

    def test_source_chain_empty_without_init(self, monkeypatch):
        """Without init_source_chain, add_provenance_entry is a no-op."""

        from tools.skill_provenance import (
            add_provenance_entry,
            get_recorded_chain,
            set_current_write_origin,
            reset_current_write_origin,
            BACKGROUND_REVIEW,
        )

        token = set_current_write_origin(BACKGROUND_REVIEW)
        try:
            add_provenance_entry("terminal", source_id="/path")
            chain = get_recorded_chain()
            assert chain == [], "Chain should be empty without init_source_chain"
        finally:
            reset_current_write_origin(token)
