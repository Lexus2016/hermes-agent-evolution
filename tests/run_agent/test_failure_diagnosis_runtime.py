# -*- coding: utf-8 -*-
"""Executable-seam runtime tests for #1029/#1030 multi-hypothesis diagnosis.

Drive the real ``AIAgent`` tool path (``_execute_tool_calls_sequential`` ->
``_append_guardrail_observation``) to prove the diagnosis is wired in and inert
unless ``failure_diagnosis.mode`` is set.
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


def _run_failing_call(agent, tool_name, result, call_id):
    tc = _mock_tool_call(tool_name, "{}", call_id)
    msg = SimpleNamespace(content="", tool_calls=[tc])
    messages: list = []
    with patch("run_agent.handle_function_call", return_value=result):
        agent._execute_tool_calls_sequential(msg, messages, "task-1")
    return messages


def test_diagnosis_defaults_off():
    agent = _make_agent("web_search")
    assert getattr(agent, "_failure_diagnosis_mode", "off") == "off"
    assert getattr(agent, "_hypothesis_history", None) is None
    messages = _run_failing_call(agent, "web_search", json.dumps({"error": "429 rate limit"}), "c-off")
    assert "Failure diagnosis" not in messages[0]["content"]


def test_diagnosis_multi_hypothesis_appends_ranked_list_through_seam():
    agent = _make_agent("terminal", config={"failure_diagnosis": {"mode": "multi-hypothesis"}})
    assert agent._failure_diagnosis_mode == "multi-hypothesis"
    assert agent._hypothesis_history is not None
    messages = _run_failing_call(
        agent, "terminal", json.dumps({"exit_code": 127, "stderr": "command not found; no such file or directory"}), "c-multi"
    )
    content = messages[0]["content"]
    assert "Failure diagnosis" in content
    assert "1)" in content


def test_diagnosis_reflect_mode_single_hypothesis():
    agent = _make_agent("web_search", config={"failure_diagnosis": {"mode": "reflect"}})
    assert agent._failure_diagnosis_mode == "reflect"
    messages = _run_failing_call(agent, "web_search", json.dumps({"error": "429 rate limit"}), "c-reflect")
    content = messages[0]["content"]
    assert "Failure diagnosis" in content
    assert "2)" not in content


def test_diagnosis_inert_on_success():
    agent = _make_agent("web_search", config={"failure_diagnosis": {"mode": "multi-hypothesis"}})
    tc = _mock_tool_call("web_search", "{}", "c-ok")
    msg = SimpleNamespace(content="", tool_calls=[tc])
    messages: list = []
    with patch("run_agent.handle_function_call", return_value=json.dumps({"results": ["ok"]})):
        agent._execute_tool_calls_sequential(msg, messages, "task-1")
    assert "Failure diagnosis" not in messages[0]["content"]


def test_invalid_mode_falls_back_to_off():
    agent = _make_agent("web_search", config={"failure_diagnosis": {"mode": "garbage"}})
    assert agent._failure_diagnosis_mode == "off"
