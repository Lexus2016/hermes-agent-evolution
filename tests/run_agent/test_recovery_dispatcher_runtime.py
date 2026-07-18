# -*- coding: utf-8 -*-
"""Executable-seam runtime tests for the #1027 recovery strategy dispatcher.

These drive the real ``AIAgent`` tool-execution path (``_execute_tool_calls_
sequential`` -> ``_append_guardrail_observation``) to prove the dispatcher is
actually wired in — not merely importable — and that it is inert unless the
``tool_failure_recovery.enabled`` config gate is set.
"""

import json
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from run_agent import AIAgent


def _make_tool_defs(*names: str) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": f"{name} tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for name in names
    ]


def _mock_tool_call(name, arguments="{}", call_id=None):
    return SimpleNamespace(
        id=call_id or f"call_{uuid.uuid4().hex[:8]}",
        type="function",
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _make_agent(*tool_names: str, config: dict | None = None) -> AIAgent:
    with (
        patch("run_agent.get_tool_definitions", return_value=_make_tool_defs(*tool_names)),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("hermes_cli.config.load_config", return_value=config or {}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            max_iterations=10,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    agent.client = MagicMock()
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.tool_delay = 0
    agent.compression_enabled = False
    agent.save_trajectories = False
    return agent


def _run_failing_call(agent: AIAgent, tool_name: str, result: str, call_id: str):
    tc = _mock_tool_call(tool_name, "{}", call_id)
    msg = SimpleNamespace(content="", tool_calls=[tc])
    messages: list = []
    with patch("run_agent.handle_function_call", return_value=result):
        agent._execute_tool_calls_sequential(msg, messages, "task-1")
    return messages


def test_recovery_gate_defaults_off_so_seam_is_inert():
    agent = _make_agent("read_file")  # no tool_failure_recovery config
    assert getattr(agent, "_failure_recovery_enabled", False) is False
    messages = _run_failing_call(
        agent,
        "read_file",
        json.dumps({"error": "No such file or directory: /x/y"}),
        "c-off",
    )
    assert len(messages) == 1
    assert "Recovery strategy" not in messages[0]["content"]


def test_recovery_gate_enabled_appends_directive_through_real_seam():
    agent = _make_agent("read_file", config={"tool_failure_recovery": {"enabled": True}})
    assert agent._failure_recovery_enabled is True
    messages = _run_failing_call(
        agent,
        "read_file",
        json.dumps({"error": "No such file or directory: /x/y"}),
        "c-on",
    )
    assert len(messages) == 1
    content = messages[0]["content"]
    # A not_found failure dispatches to verify_target.
    assert "Recovery strategy: verify_target" in content
    # exactly one recovery line even though loop-guard guidance may also append
    assert content.count("Recovery strategy") == 1


def test_recovery_gate_enabled_is_inert_on_successful_call():
    agent = _make_agent("read_file", config={"tool_failure_recovery": {"enabled": True}})
    tc = _mock_tool_call("read_file", "{}", "c-ok")
    msg = SimpleNamespace(content="", tool_calls=[tc])
    messages: list = []
    with patch("run_agent.handle_function_call", return_value=json.dumps({"content": "hello"})):
        agent._execute_tool_calls_sequential(msg, messages, "task-1")
    assert "Recovery strategy" not in messages[0]["content"]
