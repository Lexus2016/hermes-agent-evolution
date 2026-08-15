"""Unit tests for hybrid workflow optimization (select > generate > edit) (#2254)."""

import pytest

from agent.workflow_optimization import (
    OptimizationTier,
    WorkflowRoutingDecision,
    route_workflow,
)


class TestWorkflowOptimization:
    """Test the select > generate > edit workflow plasticity hierarchy."""

    def test_select_proven_delegation_pattern(self):
        delegations = [
            {"task_type": "security_audit", "role": "Auditor", "success_rate": 0.95},
            {"task_type": "translation", "role": "Translator", "success_rate": 0.40},
        ]
        decision = route_workflow(
            task="Perform a security_audit on the auth endpoints",
            available_delegations=delegations,
        )
        assert decision.tier == OptimizationTier.SELECT
        assert decision.asset_type == "delegation_pattern"
        assert decision.asset_id == "security_audit:Auditor"
        assert decision.confidence == 0.95

    def test_select_existing_skill(self):
        skills = [
            {"name": "github-pr-workflow", "description": "Manage GitHub PRs and CI"},
            {"name": "docker-build", "description": "Build container images"},
        ]
        decision = route_workflow(
            task="Open a new PR using github-pr-workflow",
            available_skills=skills,
        )
        assert decision.tier == OptimizationTier.SELECT
        assert decision.asset_type == "skill"
        assert decision.asset_id == "github-pr-workflow"

    def test_generate_when_no_asset_matches(self):
        decision = route_workflow(
            task="Design a quantum circuit simulation algorithm",
            available_skills=[{"name": "email-sender"}],
            available_delegations=[],
        )
        assert decision.tier == OptimizationTier.GENERATE
        assert decision.asset_type is None
        assert decision.in_execution_edit_allowed is False

    def test_in_flight_stability_vs_edit(self):
        # 1. Low uncertainty mid-flight: stay stable
        stable = route_workflow(
            task="Executing step 3 of build",
            is_in_flight=True,
            uncertainty=0.2,
        )
        assert stable.tier == OptimizationTier.SELECT
        assert stable.in_execution_edit_allowed is False

        # 2. High uncertainty mid-flight: dynamic edit allowed
        uncertain = route_workflow(
            task="Encountered unexpected network timeout and altered schema",
            is_in_flight=True,
            uncertainty=0.85,
        )
        assert uncertain.tier == OptimizationTier.EDIT
        assert uncertain.in_execution_edit_allowed is True
        assert "dynamic graph edit" in uncertain.reasoning

    def test_decision_serialization(self):
        dec = WorkflowRoutingDecision(
            tier=OptimizationTier.SELECT,
            asset_type="skill",
            asset_id="web-search",
            confidence=0.88,
            reasoning="Exact match",
        )
        d = dec.to_dict()
        assert d["tier"] == "select"
        assert d["asset_id"] == "web-search"
        assert d["confidence"] == 0.88
