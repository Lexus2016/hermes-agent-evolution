"""Unit and integration tests for Issue #3093: Toolset mismatch and capability awareness."""

from unittest.mock import MagicMock, patch

import pytest
from agent.prompt_builder import (
    RECOVERY_BEFORE_REFUSAL_GUIDANCE,
    RESTRICTED_TOOLSET_GUIDANCE,
    TASK_COMPLETION_GUIDANCE,
)
from agent.system_prompt import build_system_prompt_parts
from run_agent import AIAgent
from toolsets import resolve_toolset, TOOLSETS
from tools.delegate_tool import _goal_needs_file, _build_child_agent


def _make_agent(valid_tool_names=None, platform="cli"):
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            provider="openrouter",
            model="anthropic/claude-3-5-sonnet",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            platform=platform,
        )
        agent.client = MagicMock()
        if valid_tool_names is not None:
            agent.valid_tool_names = set(valid_tool_names)
        return agent


class TestIssue3093ToolsetGuidance:
    def test_api_server_toolset_includes_repo_map(self):
        """hermes-api-server toolset includes repo_map tool."""
        tools = resolve_toolset("hermes-api-server")
        assert "repo_map" in tools
        assert "read_file" in tools
        assert "write_file" in tools

    def test_full_session_injects_task_completion_guidance(self):
        """When agent has terminal or write tools, TASK_COMPLETION_GUIDANCE is injected."""
        agent = _make_agent(
            valid_tool_names={"terminal", "read_file", "write_file", "web_search"}
        )
        parts = build_system_prompt_parts(agent)
        stable = parts["stable"]
        assert TASK_COMPLETION_GUIDANCE in stable
        assert RESTRICTED_TOOLSET_GUIDANCE not in stable

    def test_restricted_session_injects_restricted_toolset_guidance(self):
        """When agent has tools but lacks terminal/write tools (e.g. web-only/safe), RESTRICTED_TOOLSET_GUIDANCE is injected."""
        agent = _make_agent(
            valid_tool_names={"web_search", "web_extract", "vision_analyze", "clarify"}
        )
        parts = build_system_prompt_parts(agent)
        stable = parts["stable"]
        assert RESTRICTED_TOOLSET_GUIDANCE in stable
        assert TASK_COMPLETION_GUIDANCE not in stable

    def test_recovery_before_refusal_includes_scoped_boundaries(self):
        """RECOVERY_BEFORE_REFUSAL_GUIDANCE includes guidance on scoped toolset boundaries."""
        assert "Scoped toolset without terminal/write access" in RECOVERY_BEFORE_REFUSAL_GUIDANCE


class TestIssue3093DelegationToolsetInheritance:
    def test_goal_needs_file_heuristics(self):
        """_goal_needs_file detects filesystem-oriented goals."""
        assert _goal_needs_file("Create a new Python file src/main.py") is True
        assert _goal_needs_file("Patch the auth handler in auth.py") is True
        assert _goal_needs_file("Edit the config file") is True
        assert _goal_needs_file("Search for information about neural networks") is False

    def test_child_agent_auto_inherits_file_toolset(self):
        """Child subagent auto-adds 'file' toolset when goal needs filesystem and parent has file access."""
        parent_agent = _make_agent(
            valid_tool_names={"read_file", "write_file", "web_search", "delegate_task"}
        )
        parent_agent.enabled_toolsets = ["web", "file", "delegation"]
        parent_agent.disabled_toolsets = []

        with (
            patch("run_agent.AIAgent") as mock_child_cls,
            patch("tools.delegate_tool._load_config", return_value={}),
        ):
            mock_child = MagicMock()
            mock_child_cls.return_value = mock_child

            _build_child_agent(
                task_index=1,
                goal="Write and patch the test files in tests/",
                context="",
                toolsets=["web"],
                model=None,
                max_iterations=10,
                task_count=1,
                parent_agent=parent_agent,
            )

            # Assert mock_child_cls was called with enabled_toolsets containing 'file'
            call_kwargs = mock_child_cls.call_args.kwargs
            enabled = call_kwargs.get("enabled_toolsets", [])
            assert "file" in enabled
