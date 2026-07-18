# -*- coding: utf-8 -*-
"""Unit tests for the plan feasibility gate (#1032)."""

from __future__ import annotations

from agent.plan_schema import Plan, Step
from agent.plan_feasibility import (
    FeasibilityStatus,
    PlanFeasibilityReport,
    check_plan_feasibility,
    maybe_validate_plan,
)


def _plan(*intents: str) -> Plan:
    steps = [
        Step(tool_call_intent=intent, rationale="r", expected_observation="e")
        for intent in intents
    ]
    return Plan(steps=steps, goal="g")


def test_read_file_missing_path_is_infeasible():
    plan = _plan("read_file(agent/does_not_exist.py)")
    report = check_plan_feasibility(plan, file_exists=lambda p: False)
    assert report.feasible is False
    assert len(report.blockers) == 1
    blocker = report.blockers[0]
    assert blocker.status is FeasibilityStatus.INFEASIBLE
    assert "does_not_exist" in blocker.reason
    assert blocker.metadata["path"] == "agent/does_not_exist.py"


def test_read_file_existing_path_is_feasible():
    plan = _plan("read_file(agent/plan_schema.py)")
    report = check_plan_feasibility(plan, file_exists=lambda p: True)
    assert report.feasible is True
    assert report.steps[0].status is FeasibilityStatus.FEASIBLE


def test_free_text_intent_is_uncertain_not_a_blocker():
    plan = _plan("search the web for FLARE benchmark numbers")
    report = check_plan_feasibility(plan, file_exists=lambda p: False)
    assert report.feasible is True
    assert report.steps[0].status is FeasibilityStatus.UNCERTAIN


def test_unavailable_tool_is_infeasible_only_when_toolset_supplied():
    plan = _plan("nonexistent_tool(x)")
    # Without a toolset, tool availability is not checked -> not a blocker.
    report_no_tools = check_plan_feasibility(plan, file_exists=lambda p: True)
    assert report_no_tools.feasible is True
    # With a toolset that lacks the tool -> INFEASIBLE.
    report = check_plan_feasibility(
        plan, file_exists=lambda p: True, available_tools={"read_file", "web_search"}
    )
    assert report.feasible is False
    assert report.blockers[0].metadata["tool"] == "nonexistent_tool"


def test_available_tool_with_existing_file_is_feasible():
    plan = _plan("read_file(agent/plan_schema.py)")
    report = check_plan_feasibility(
        plan, file_exists=lambda p: True, available_tools={"read_file"}
    )
    assert report.steps[0].status is FeasibilityStatus.FEASIBLE


def test_write_file_missing_parent_is_uncertain_not_blocker():
    plan = _plan("write_file(/definitely/missing/dir/out.txt)")
    report = check_plan_feasibility(plan, file_exists=lambda p: False)
    # A missing write parent is a soft signal, never a hard blocker.
    assert report.feasible is True
    assert report.steps[0].status is FeasibilityStatus.UNCERTAIN


def test_read_file_non_path_argument_is_not_treated_as_path():
    # "foo" has no slash/dot -> not confidently a path -> UNCERTAIN, not blocker.
    plan = _plan("read_file(foo)")
    report = check_plan_feasibility(plan, file_exists=lambda p: False)
    assert report.feasible is True
    assert report.steps[0].status is FeasibilityStatus.UNCERTAIN


def test_first_arg_path_strips_quotes_and_keyword_form():
    plan = _plan('read_file(path="agent/missing.py", offset=1)')
    report = check_plan_feasibility(plan, file_exists=lambda p: False)
    assert report.blockers[0].metadata["path"] == "agent/missing.py"


def test_mixed_plan_reports_only_real_blockers():
    plan = _plan(
        "read_file(agent/missing_a.py)",  # infeasible
        "search the web for X",  # uncertain
        "read_file(agent/plan_schema.py)",  # feasible (exists stub True below)
    )

    def exists(p: str) -> bool:
        return "plan_schema" in p

    report = check_plan_feasibility(plan, file_exists=exists)
    statuses = [s.status for s in report.steps]
    assert statuses == [
        FeasibilityStatus.INFEASIBLE,
        FeasibilityStatus.UNCERTAIN,
        FeasibilityStatus.FEASIBLE,
    ]
    assert len(report.blockers) == 1


def test_report_to_dict_shape():
    plan = _plan("read_file(agent/missing.py)")
    report = check_plan_feasibility(plan, file_exists=lambda p: False)
    data = report.to_dict()
    assert data["feasible"] is False
    assert data["blocker_count"] == 1
    assert data["steps"][0]["status"] == "infeasible"


def test_maybe_validate_disabled_is_noop():
    plan = _plan("read_file(agent/missing.py)")
    result = maybe_validate_plan(plan, enabled=False, file_exists=lambda p: False)
    assert result is None
    assert "feasibility" not in plan.metadata


def test_maybe_validate_enabled_records_report_on_metadata():
    plan = _plan("read_file(agent/missing.py)")
    report = maybe_validate_plan(plan, enabled=True, file_exists=lambda p: False)
    assert isinstance(report, PlanFeasibilityReport)
    assert plan.metadata["feasibility"]["feasible"] is False
    assert plan.metadata["feasibility"]["blocker_count"] == 1


def test_maybe_validate_none_plan_is_noop():
    assert maybe_validate_plan(None, enabled=True) is None


def test_empty_plan_is_feasible():
    plan = Plan(steps=[Step(tool_call_intent="noop step", rationale="r", expected_observation="e")], goal="g")
    report = check_plan_feasibility(plan)
    assert report.feasible is True
