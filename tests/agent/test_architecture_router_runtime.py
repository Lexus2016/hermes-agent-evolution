# -*- coding: utf-8 -*-
"""Executable-seam runtime tests for #1139 architecture router.

Drive the real ``run_conversation`` turn loop to prove the architecture-router
pre-flight in ``agent/conversation_loop.py`` runs (recording a routing decision
to telemetry) when enabled, and is a no-op otherwise.
"""

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


_PARALLEL_TASK = (
    "For each of these companies compare their revenue respectively: "
    "1. Apple\n2. Microsoft\n3. Google — analyze all of them"
)


def test_router_off_records_nothing():
    cfg = {}
    agent = _make_agent(config=cfg)
    result = _run_once(agent, _PARALLEL_TASK, cfg)
    assert result["final_response"] == "done"
    assert getattr(agent, "_architecture_route", None) is None


def test_router_on_records_decision_and_telemetry():
    cfg = {"architecture_router": {"enabled": True}}
    agent = _make_agent(config=cfg)
    _run_once(agent, _PARALLEL_TASK, cfg)
    decision = getattr(agent, "_architecture_route", None)
    assert decision is not None
    # a parallelizable task should route to the centralized orchestrator
    assert decision.architecture.value == "centralized_orchestrator"
    tel = getattr(agent, "_architecture_router_telemetry", None)
    assert tel is not None
    assert len(tel) == 1


def test_router_on_sequential_task_avoids_multi_agent():
    cfg = {"architecture_router": {"enabled": True}}
    agent = _make_agent(config=cfg)
    task = "First read the file, then transform it, after that write output, and finally verify"
    _run_once(agent, task, cfg)
    decision = getattr(agent, "_architecture_route", None)
    assert decision is not None
    assert decision.architecture.value in {"plan_and_execute", "single_agent"}
    assert decision.max_workers == 1
