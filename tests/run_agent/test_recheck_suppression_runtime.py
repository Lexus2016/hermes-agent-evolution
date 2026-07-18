# -*- coding: utf-8 -*-
"""Executable-seam runtime tests for #1041 recheck suppression.

Drive the real ``AIAgent`` tool path so an immediate identical repeat of a
successful read-only call is suppressed at the guardrail ``before_call`` seam —
without executing the tool again and without halting the turn — and only when
``recheck_suppression.enabled`` is set.
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


def _two_identical_reads(agent):
    args = json.dumps({"path": "agent/plan_schema.py"})
    calls = [
        _mock_tool_call("read_file", args, "c-1"),
        _mock_tool_call("read_file", args, "c-2"),
    ]
    msg = SimpleNamespace(content="", tool_calls=calls)
    messages: list = []
    executed = []

    def fake_handle(name, a, task_id, **kwargs):
        executed.append(kwargs.get("tool_call_id"))
        return json.dumps({"content": "file body"})

    with patch("run_agent.handle_function_call", side_effect=fake_handle):
        agent._execute_tool_calls_sequential(msg, messages, "task-1")
    return messages, executed


def test_recheck_suppression_defaults_off_executes_both():
    agent = _make_agent("read_file")  # no recheck config
    assert agent._tool_guardrails.recheck_controller is not None
    assert agent._tool_guardrails.recheck_controller.enabled is False
    messages, executed = _two_identical_reads(agent)
    # both calls execute; no suppression
    assert executed == ["c-1", "c-2"]
    assert all("recheck_suppressed" not in m["content"] for m in messages)


def test_recheck_suppression_enabled_suppresses_immediate_repeat():
    agent = _make_agent("read_file", config={"recheck_suppression": {"enabled": True}})
    assert agent._tool_guardrails.recheck_controller.enabled is True
    messages, executed = _two_identical_reads(agent)
    # only the first executes; the immediate repeat is suppressed
    assert executed == ["c-1"]
    contents = {m["tool_call_id"]: m["content"] for m in messages}
    assert "recheck_suppressed" in contents["c-2"]
    assert "recheck_suppressed" not in contents["c-1"]
    # the turn is NOT halted by a suppression
    assert agent._tool_guardrail_halt_decision is None
    # calibration log recorded the suppression
    log = agent._tool_guardrails.recheck_controller.calibration_log
    assert log.suppressed_count == 1


def test_recheck_suppression_does_not_touch_mutating_tools():
    agent = _make_agent("write_file", config={"recheck_suppression": {"enabled": True}})
    args = json.dumps({"path": "x.txt", "content": "a"})
    calls = [
        _mock_tool_call("write_file", args, "w-1"),
        _mock_tool_call("write_file", args, "w-2"),
    ]
    msg = SimpleNamespace(content="", tool_calls=calls)
    messages: list = []
    executed = []

    def fake_handle(name, a, task_id, **kwargs):
        executed.append(kwargs.get("tool_call_id"))
        return json.dumps({"success": True})

    with patch("run_agent.handle_function_call", side_effect=fake_handle):
        agent._execute_tool_calls_sequential(msg, messages, "task-1")
    # mutating tool: both execute, never suppressed
    assert executed == ["w-1", "w-2"]
