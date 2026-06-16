"""Tests for agent/cost_aware_planner.py — the cost-aware orchestration core.

These exercise the planner against the real pricing engine (no mocks); the
models used (claude-haiku-4-5 / claude-opus-4-8 / claude-sonnet-4-6) all have
deterministic official-docs-snapshot pricing, so the cost arithmetic is exact.
"""

from decimal import Decimal

import pytest

from agent.cost_aware_planner import (
    OrchestrationPlan,
    PlanStep,
    PlanValidationError,
    StepCostEstimate,
    TokenFootprint,
    ToolRoute,
    format_plan_summary,
    plan_orchestration,
)


def _opus_input_cost(input_tokens: int) -> Decimal:
    # claude-opus-4-8 input is $5.00 / 1M tokens.
    return Decimal(input_tokens) * Decimal("5.00") / Decimal("1000000")


# ---------------------------------------------------------------------------
# Topology: waves, ordering, parallelism
# ---------------------------------------------------------------------------


def test_independent_steps_form_a_single_parallel_wave():
    steps = [
        PlanStep(id="a", footprint=TokenFootprint(input_tokens=1000)),
        PlanStep(id="b", footprint=TokenFootprint(input_tokens=1000)),
    ]
    plan = plan_orchestration(steps)
    assert plan.waves == (("a", "b"),)
    assert plan.max_parallelism == 2


def test_dependency_chain_serializes_into_ordered_waves():
    steps = [
        PlanStep(id="fetch", footprint=TokenFootprint(input_tokens=1000)),
        PlanStep(id="parse", depends_on=("fetch",), footprint=TokenFootprint(input_tokens=1000)),
        PlanStep(id="report", depends_on=("parse",), footprint=TokenFootprint(input_tokens=1000)),
    ]
    plan = plan_orchestration(steps)
    assert plan.waves == (("fetch",), ("parse",), ("report",))
    assert plan.serial_order == ("fetch", "parse", "report")
    assert plan.max_parallelism == 1


def test_diamond_topology_groups_independent_branches_in_one_wave():
    # root -> {left, right} -> join
    steps = [
        PlanStep(id="root"),
        PlanStep(id="left", depends_on=("root",)),
        PlanStep(id="right", depends_on=("root",)),
        PlanStep(id="join", depends_on=("left", "right")),
    ]
    plan = plan_orchestration(steps)
    assert plan.waves == (("root",), ("left", "right"), ("join",))
    assert plan.max_parallelism == 2
    # Every step appears exactly once in the serial order.
    assert sorted(plan.serial_order) == ["join", "left", "right", "root"]


def test_duplicate_dependency_edge_does_not_strand_step():
    steps = [
        PlanStep(id="a"),
        PlanStep(id="b", depends_on=("a", "a")),  # repeated edge
    ]
    plan = plan_orchestration(steps)
    assert plan.waves == (("a",), ("b",))


def test_empty_plan_is_valid_and_zero_cost():
    plan = plan_orchestration([])
    assert plan.waves == ()
    assert plan.serial_order == ()
    assert plan.total_estimated_cost_usd == Decimal("0")
    assert plan.max_parallelism == 0


# ---------------------------------------------------------------------------
# Validation errors
# ---------------------------------------------------------------------------


def test_duplicate_step_id_raises():
    steps = [PlanStep(id="dup"), PlanStep(id="dup")]
    with pytest.raises(PlanValidationError, match="duplicate step id"):
        plan_orchestration(steps)


def test_dangling_dependency_raises():
    steps = [PlanStep(id="a", depends_on=("ghost",))]
    with pytest.raises(PlanValidationError, match="unknown step"):
        plan_orchestration(steps)


def test_self_dependency_raises():
    steps = [PlanStep(id="a", depends_on=("a",))]
    with pytest.raises(PlanValidationError, match="depends on itself"):
        plan_orchestration(steps)


def test_dependency_cycle_raises():
    steps = [
        PlanStep(id="a", depends_on=("b",)),
        PlanStep(id="b", depends_on=("a",)),
    ]
    with pytest.raises(PlanValidationError, match="cycle"):
        plan_orchestration(steps)


# ---------------------------------------------------------------------------
# Cost estimation built on the pricing engine
# ---------------------------------------------------------------------------


def test_step_cost_matches_pricing_engine_for_known_model():
    steps = [
        PlanStep(
            id="one",
            footprint=TokenFootprint(input_tokens=1_000_000),
            routes=(ToolRoute(model="claude-opus-4-8", provider="anthropic"),),
        )
    ]
    plan = plan_orchestration(steps)
    est = plan.step_estimates["one"]
    assert est.cost_status == "estimated"
    # 1M input tokens at $5/M = exactly $5.00.
    assert est.estimated_cost_usd == _opus_input_cost(1_000_000)
    assert plan.total_estimated_cost_usd == est.estimated_cost_usd


def test_cheapest_route_is_recommended():
    # Haiku ($1/M in) is cheaper than Opus ($5/M in) for the same footprint.
    steps = [
        PlanStep(
            id="cheap_pick",
            footprint=TokenFootprint(input_tokens=10_000, output_tokens=2_000),
            routes=(
                ToolRoute(model="claude-opus-4-8", provider="anthropic"),
                ToolRoute(model="claude-haiku-4-5", provider="anthropic"),
            ),
        )
    ]
    plan = plan_orchestration(steps)
    est = plan.step_estimates["cheap_pick"]
    assert est.recommended_route.model == "claude-haiku-4-5"
    # Haiku: 10k in @ $1/M + 2k out @ $5/M = 0.01 + 0.01 = 0.02
    assert est.estimated_cost_usd == Decimal("0.02")
    # Both routes evaluated, cheapest first.
    assert len(est.evaluated_routes) == 2
    assert est.evaluated_routes[0][0].model == "claude-haiku-4-5"


def test_unknown_pricing_step_flagged_not_summed():
    steps = [
        PlanStep(
            id="known",
            footprint=TokenFootprint(input_tokens=1_000_000),
            routes=(ToolRoute(model="claude-opus-4-8", provider="anthropic"),),
        ),
        PlanStep(
            id="mystery",
            footprint=TokenFootprint(input_tokens=1_000_000),
            routes=(ToolRoute(model="totally-unknown-model-xyz", provider="nobody"),),
        ),
    ]
    plan = plan_orchestration(steps)
    assert plan.unknown_cost_steps == ("mystery",)
    assert plan.step_estimates["mystery"].estimated_cost_usd is None
    assert plan.step_estimates["mystery"].cost_status == "unknown"
    # Total only includes the known step.
    assert plan.total_estimated_cost_usd == _opus_input_cost(1_000_000)


def test_step_without_routes_is_costless_in_topology():
    # A non-LLM tool (no routes) still participates in the DAG but prices unknown.
    steps = [
        PlanStep(id="fetch_url"),  # no routes, no footprint
        PlanStep(
            id="summarize",
            depends_on=("fetch_url",),
            footprint=TokenFootprint(input_tokens=1_000_000),
            routes=(ToolRoute(model="claude-opus-4-8", provider="anthropic"),),
        ),
    ]
    plan = plan_orchestration(steps)
    assert plan.waves == (("fetch_url",), ("summarize",))
    assert "fetch_url" in plan.unknown_cost_steps
    assert plan.step_estimates["fetch_url"].recommended_route is None


def test_critical_path_cost_takes_max_per_wave():
    # Wave with two parallel steps: critical path counts only the costlier one.
    steps = [
        PlanStep(
            id="big",
            footprint=TokenFootprint(input_tokens=1_000_000),
            routes=(ToolRoute(model="claude-opus-4-8", provider="anthropic"),),
        ),
        PlanStep(
            id="small",
            footprint=TokenFootprint(input_tokens=10_000),
            routes=(ToolRoute(model="claude-opus-4-8", provider="anthropic"),),
        ),
    ]
    plan = plan_orchestration(steps)
    # Both in one wave -> total = big + small, critical path = big only.
    big = _opus_input_cost(1_000_000)
    small = _opus_input_cost(10_000)
    assert plan.total_estimated_cost_usd == big + small
    assert plan.critical_path_cost_usd == big


# ---------------------------------------------------------------------------
# Summary formatting
# ---------------------------------------------------------------------------


def test_format_plan_summary_mentions_waves_and_total():
    steps = [
        PlanStep(
            id="a",
            footprint=TokenFootprint(input_tokens=1_000_000),
            routes=(ToolRoute(model="claude-opus-4-8", provider="anthropic"),),
        ),
        PlanStep(id="b", depends_on=("a",)),
    ]
    plan = plan_orchestration(steps)
    summary = format_plan_summary(plan)
    assert "2 waves" in summary
    assert "wave 1" in summary
    assert "Total estimated cost" in summary
    assert "Unknown-cost steps: b" in summary


def test_format_plan_summary_empty_plan():
    assert format_plan_summary(plan_orchestration([])) == "empty plan (no steps)"


def test_returned_types_are_dataclasses():
    plan = plan_orchestration([PlanStep(id="x")])
    assert isinstance(plan, OrchestrationPlan)
    assert isinstance(plan.step_estimates["x"], StepCostEstimate)
