# -*- coding: utf-8 -*-
"""Executable-seam runtime tests for #1138 task-decoupled planning.

Drive the real ``run_conversation`` turn loop to prove the task-decoupling
pre-flight in ``agent/conversation_loop.py`` runs (building a sub-goal DAG on the
agent) when enabled + the task is long, and is a no-op otherwise.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from run_agent import AIAgent


def _mock_response(content="done", finish_reason="stop"):
    msg = SimpleNamespace(content=content, tool_calls=None)
    choice = SimpleNamespace(message=msg, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], model="test/model", usage=None)


def _make_agent(config: dict | None = None) -> AIAgent:
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("hermes_cli.config.load_config", return_value=config or {}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            max_iterations=3,
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


def _run_once(agent, task: str, config: dict):
    agent.client.chat.completions.create.side_effect = [_mock_response("done")]
    with (
        patch("hermes_cli.config.load_config_readonly", return_value=config),
        patch("hermes_cli.config.load_config", return_value=config),
        patch.object(agent, "_persist_session", create=True),
        patch.object(agent, "_save_trajectory", create=True),
        patch.object(agent, "_cleanup_task_resources", create=True),
    ):
        return agent.run_conversation(task)


_LONG_TASK = (
    "1. read the config file\n2. transform the data based on it\n"
    "3. write the output\n4. then verify the result is correct"
)


def test_task_decoupling_off_leaves_no_dag():
    cfg = {}  # disabled by default
    agent = _make_agent(config=cfg)
    result = _run_once(agent, _LONG_TASK, cfg)
    assert result["final_response"] == "done"
    assert getattr(agent, "_subgoal_dag", None) is None


def test_task_decoupling_on_builds_dag_for_long_task():
    cfg = {"task_decoupling": {"enabled": True, "min_task_chars": 20}}
    agent = _make_agent(config=cfg)
    _run_once(agent, _LONG_TASK, cfg)
    dag = getattr(agent, "_subgoal_dag", None)
    assert dag is not None
    # the numbered task decomposed into multiple ordered sub-goals
    assert len(dag.nodes) >= 2


def test_task_decoupling_on_skips_short_task():
    cfg = {"task_decoupling": {"enabled": True, "min_task_chars": 500}}
    agent = _make_agent(config=cfg)
    _run_once(agent, "short task", cfg)
    assert getattr(agent, "_subgoal_dag", None) is None
