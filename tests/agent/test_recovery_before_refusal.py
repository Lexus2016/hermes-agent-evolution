"""Tests for recovery-before-refusal guidance (#1356).

Verifies the guidance constant exists with the right content and that
build_system_prompt_parts injects it only when tools are loaded.
"""

from types import SimpleNamespace
from unittest.mock import patch

from agent.prompt_builder import RECOVERY_BEFORE_REFUSAL_GUIDANCE
from agent.system_prompt import build_system_prompt_parts


def _make_agent(**overrides):
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


def _stable_prompt(agent):
    with (
        patch("run_agent.load_soul_md", return_value=""),
        patch("run_agent.build_nous_subscription_prompt", return_value=""),
        patch("run_agent.build_environment_hints", return_value=""),
        patch("run_agent.build_context_files_prompt", return_value=""),
    ):
        return build_system_prompt_parts(agent)["stable"]


class TestRecoveryBeforeRefusalConstant:
    def test_constant_has_heading(self):
        assert "# Recovery before refusal" in RECOVERY_BEFORE_REFUSAL_GUIDANCE

    def test_constant_mentions_alternative_path(self):
        assert "alternative" in RECOVERY_BEFORE_REFUSAL_GUIDANCE.lower()

    def test_constant_mentions_existing_tool(self):
        assert "existing tool" in RECOVERY_BEFORE_REFUSAL_GUIDANCE.lower()

    def test_constant_has_examples(self):
        assert "Examples:" in RECOVERY_BEFORE_REFUSAL_GUIDANCE
        assert "search_files" in RECOVERY_BEFORE_REFUSAL_GUIDANCE
        assert "terminal" in RECOVERY_BEFORE_REFUSAL_GUIDANCE

    def test_constant_is_concise(self):
        # Should be under 800 chars — this is cached system prompt text
        assert len(RECOVERY_BEFORE_REFUSAL_GUIDANCE) < 800


class TestRecoveryBeforeRefusalInjection:
    def test_injected_when_tools_loaded(self):
        agent = _make_agent(valid_tool_names=["read_file", "terminal"])
        stable = _stable_prompt(agent)
        assert "Recovery before refusal" in stable
        assert "alternative" in stable.lower()

    def test_absent_when_no_tools(self):
        agent = _make_agent(valid_tool_names=[])
        stable = _stable_prompt(agent)
        assert "Recovery before refusal" not in stable
