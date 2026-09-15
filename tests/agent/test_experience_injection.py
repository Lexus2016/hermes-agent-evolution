"""Tests for experience-bank pattern injection into the system prompt.

Covers the context-tier block in ``build_system_prompt_parts`` gated by
``agent._experience_injection`` (config ``agent.experience_injection``).
Patterns are seeded via the real bank module against the hermetic
``HERMES_HOME`` fixture from conftest — no mocks of the bank itself except
the explicit raising-bank test.
"""

import agent.experience_bank as experience_bank
from types import SimpleNamespace
from unittest.mock import patch

from agent.experience_bank import ExperiencePattern, save_patterns
from agent.system_prompt import build_system_prompt_parts

PATTERNS_HEADING = "## Learned execution patterns"


def _make_agent(**overrides):
    """Minimal fake agent — same shape as tests/agent/test_system_prompt.py."""
    base = dict(
        load_soul_identity=False,
        skip_context_files=False,
        valid_tool_names=[],
        _task_completion_guidance=False,
        _tool_use_enforcement=False,
        _environment_probe=False,
        _kanban_worker_guidance="",
        _memory_store=None,
        _memory_manager=None,
        model="",
        provider="",
        platform="",
        pass_session_id=False,
        session_id="",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _build_parts(agent):
    with (
        patch("agent.prompt_builder.load_soul_md", return_value=""),
        patch("agent.prompt_builder.build_nous_subscription_prompt", return_value=""),
        patch("agent.prompt_builder.build_environment_hints", return_value=""),
        patch("agent.prompt_builder.build_context_files_prompt", return_value=""),
    ):
        return build_system_prompt_parts(agent)


def _seed_patterns():
    save_patterns([
        ExperiencePattern(
            id="tool-network",
            dimension="tool",
            category="network",
            tool="web_search",
            trigger="HTTP tool call fails with a connection error",
            guidance="Retry idempotent requests before declaring failure.",
            evidence_count=3,
            first_seen=1.0,
            last_seen=2.0,
        )
    ])


class TestExperienceInjection:
    def test_flag_absent_no_block_even_with_seeded_patterns(self):
        _seed_patterns()
        agent = _make_agent()  # no _experience_injection attribute at all
        parts = _build_parts(agent)
        assert PATTERNS_HEADING not in parts["context"]

    def test_flag_false_no_block_even_with_seeded_patterns(self):
        _seed_patterns()
        agent = _make_agent(_experience_injection=False)
        parts = _build_parts(agent)
        assert PATTERNS_HEADING not in parts["context"]

    def test_flag_true_seeded_patterns_block_in_context_only(self):
        _seed_patterns()
        agent = _make_agent(
            _experience_injection=True,
            valid_tool_names=["web_search"],
        )
        parts = _build_parts(agent)
        assert PATTERNS_HEADING in parts["context"]
        assert "probe connectivity" in parts["context"]
        assert PATTERNS_HEADING not in parts["stable"]
        assert PATTERNS_HEADING not in parts["volatile"]

    def test_flag_true_empty_bank_matches_disabled(self):
        agent_on = _make_agent(_experience_injection=True)
        agent_off = _make_agent(_experience_injection=False)
        parts_on = _build_parts(agent_on)
        parts_off = _build_parts(agent_off)
        assert PATTERNS_HEADING not in parts_on["context"]
        assert parts_on["context"] == parts_off["context"]

    def test_raising_bank_never_breaks_prompt_build(self, monkeypatch):
        def _boom(*args, **kwargs):
            raise RuntimeError("bank exploded")

        monkeypatch.setattr(experience_bank, "format_patterns_prompt", _boom)
        agent = _make_agent(_experience_injection=True)
        parts = _build_parts(agent)
        assert PATTERNS_HEADING not in parts["context"]
        assert set(parts) == {"stable", "context", "volatile"}

    def test_block_is_cached_within_session(self):
        """Prompt rebuilds reuse the first resolved experience bytes.

        Перебудови промпта повторно використовують перший сформований блок.
        """
        _seed_patterns()
        agent = _make_agent(
            _experience_injection=True,
            valid_tool_names=["web_search"],
        )
        first = _build_parts(agent)

        save_patterns([
            ExperiencePattern(
                id="context-not-found",
                dimension="context",
                category="not_found",
                trigger="missing path",
                guidance="tampered stored guidance",
                evidence_count=9,
                first_seen=3.0,
                last_seen=4.0,
            )
        ])
        second = _build_parts(agent)

        assert first["context"] == second["context"]
        assert "probe connectivity" in second["context"]
        assert "verify it exists" not in second["context"]

    def test_only_enabled_tool_patterns_are_injected(self):
        save_patterns([
            ExperiencePattern(
                id="tool-network-malicious",
                dimension="tool",
                category="network",
                tool="delete_everything",
                evidence_count=10,
            ),
            ExperiencePattern(
                id="tool-network-valid",
                dimension="tool",
                category="network",
                tool="web_search",
                evidence_count=5,
            ),
            ExperiencePattern(
                id="context-not-found",
                dimension="context",
                category="not_found",
                tool=None,
                evidence_count=3,
            ),
        ])
        agent = _make_agent(
            _experience_injection=True,
            valid_tool_names=["web_search"],
        )

        context = _build_parts(agent)["context"]

        assert "delete_everything" not in context
        assert "`web_search`" in context
        assert "verify it exists" in context
