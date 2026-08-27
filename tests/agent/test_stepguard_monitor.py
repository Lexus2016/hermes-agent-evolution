# -*- coding: utf-8 -*-
"""Unit tests for agent.stepguard_monitor (#3245)."""

import pytest
from agent.stepguard_monitor import (
    StepGuardDecision,
    compute_safety_risk,
    compute_utility_cost,
    evaluate_step_guardrail,
)


class TestStepGuardMonitor:
    def test_benign_command_allowed_with_high_utility(self):
        decision = evaluate_step_guardrail("terminal", {"command": "git status"})
        assert decision.allow is True
        assert decision.action == "allow"
        assert decision.safety_risk < 0.3
        assert decision.utility_cost >= 0.8

    def test_read_tool_allowed(self):
        decision = evaluate_step_guardrail("read_file", {"path": "agent/run_agent.py"})
        assert decision.allow is True
        assert decision.safety_risk == 0.0
        assert decision.utility_cost >= 0.8

    def test_destructive_root_deletion_blocked(self):
        decision = evaluate_step_guardrail("terminal", {"command": "rm -rf /"})
        assert decision.allow is False
        assert decision.action == "block"
        assert decision.safety_risk >= 0.9
        assert "StepGuard block" in decision.reason

    def test_forkbomb_blocked(self):
        decision = evaluate_step_guardrail("terminal", {"command": ":(){ :|:& };:"})
        assert decision.allow is False
        assert decision.action == "block"

    def test_system_path_write_warns_or_gates(self):
        decision = evaluate_step_guardrail("write_file", {"path": "/etc/passwd", "content": "root"})
        assert decision.safety_risk >= 0.8
