# -*- coding: utf-8 -*-
"""Executable-seam integration tests for the #1032 plan feasibility gate.

Drive the real ``AIAgent._maybe_activate_plan_mode`` seam to prove the gate is
wired into the plan-execution loop — validating a freshly built plan before any
of its steps drive a tool call — and that it is inert unless BOTH plan mode and
``plan_feasibility.enabled`` are set.
"""

from unittest.mock import MagicMock, patch

from run_agent import AIAgent
from agent.plan_schema import Plan, Step


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
            max_iterations=5,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    agent.client = None  # force the deterministic stub planner path
    agent._active_plan = None
    return agent


def _infeasible_plan() -> Plan:
    return Plan(
        steps=[
            Step(
                tool_call_intent="read_file(agent/definitely_missing_file_xyz.py)",
                rationale="need the file",
                expected_observation="file contents",
            ),
            Step(
                tool_call_intent="search the web for background",
                rationale="context",
                expected_observation="results",
            ),
        ],
        goal="do the thing",
    )


def test_plan_mode_off_seam_is_inert():
    agent = _make_agent()  # plan mode off (default)
    with patch("hermes_cli.config.load_config", return_value={}):
        agent._maybe_activate_plan_mode("do the thing")
    assert getattr(agent, "_active_plan", None) is None
    assert getattr(agent, "_plan_feasibility_report", None) is None


def test_plan_mode_on_feasibility_off_leaves_plan_unvalidated():
    cfg = {"plan_mode": True}
    agent = _make_agent(config=cfg)
    assert agent._plan_feasibility_enabled is False
    with (
        patch("hermes_cli.config.load_config", return_value=cfg),
        patch("agent.plan_mode.build_stub_plan", return_value=_infeasible_plan()),
    ):
        agent._maybe_activate_plan_mode("do the thing")
    assert agent._active_plan is not None
    assert "feasibility" not in agent._active_plan.metadata
    assert agent._plan_feasibility_report is None


def test_plan_mode_on_feasibility_on_validates_through_real_seam():
    cfg = {"plan_mode": True, "plan_feasibility": {"enabled": True}}
    agent = _make_agent(config=cfg)
    assert agent._plan_feasibility_enabled is True
    with (
        patch("hermes_cli.config.load_config", return_value=cfg),
        patch("agent.plan_mode.build_stub_plan", return_value=_infeasible_plan()),
    ):
        agent._maybe_activate_plan_mode("do the thing")
    assert agent._active_plan is not None
    report = agent._active_plan.metadata.get("feasibility")
    assert report is not None
    # The missing read_file target is caught before any tool call.
    assert report["feasible"] is False
    assert report["blocker_count"] == 1
    assert agent._plan_feasibility_report is not None
    assert agent._plan_feasibility_report.feasible is False
