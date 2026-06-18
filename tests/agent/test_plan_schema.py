"""Tests for agent.plan_schema — Step/Plan data model and inert emission seam.

Covers the issue-#290 contract: the three Step fields, ordered/immutable Plan
assembly with 1-based indexing, round-trip serialization, human-readable
rendering, and ``emit_plan`` being inert (no-op) when no plan is set — the
property that keeps the run_agent.py hook default-off.
"""

import pytest

from agent.plan_schema import Plan, Step, emit_plan


# ── Step ────────────────────────────────────────────────────────────────────

class TestStep:
    def test_holds_the_three_required_fields(self):
        step = Step(
            tool_call_intent="read_file(agent/plan_schema.py)",
            rationale="understand the current schema before editing",
            expected_observation="the module source with Step and Plan defs",
        )
        assert step.tool_call_intent == "read_file(agent/plan_schema.py)"
        assert step.rationale == "understand the current schema before editing"
        assert step.expected_observation == "the module source with Step and Plan defs"
        assert step.index is None

    def test_is_frozen(self):
        step = Step(tool_call_intent="x", rationale="y", expected_observation="z")
        with pytest.raises((AttributeError, TypeError)):
            step.tool_call_intent = "mutated"  # type: ignore[misc]

    def test_empty_intent_rejected_at_source(self):
        with pytest.raises(ValueError):
            Step(tool_call_intent="   ", rationale="y", expected_observation="z")

    def test_empty_rationale_rejected_at_source(self):
        with pytest.raises(ValueError):
            Step(tool_call_intent="x", rationale="", expected_observation="z")

    def test_empty_expected_observation_allowed(self):
        # expected_observation is the replanning yardstick (#292); a step may
        # legitimately not predict an observation yet.
        step = Step(tool_call_intent="x", rationale="y", expected_observation="")
        assert step.expected_observation == ""

    def test_with_index_returns_pinned_copy(self):
        step = Step(tool_call_intent="x", rationale="y", expected_observation="z")
        pinned = step.with_index(3)
        assert pinned.index == 3
        assert step.index is None  # original untouched (frozen copy semantics)
        assert pinned.tool_call_intent == "x"

    def test_round_trip_dict(self):
        step = Step(
            tool_call_intent="search the web for FLARE numbers",
            rationale="cite the benchmark improvement",
            expected_observation="CWQ 58% -> 73.6%",
            index=2,
        )
        restored = Step.from_dict(step.to_dict())
        assert restored == step

    def test_from_dict_tolerates_missing_optional_fields(self):
        step = Step.from_dict({"tool_call_intent": "x", "rationale": "y"})
        assert step.expected_observation == ""
        assert step.index is None

    def test_render_includes_intent_rationale_and_expectation(self):
        step = Step(
            tool_call_intent="run pytest",
            rationale="prove the change is green",
            expected_observation="all tests pass",
            index=1,
        )
        rendered = step.render()
        assert "1. run pytest" in rendered
        assert "why: prove the change is green" in rendered
        assert "expect: all tests pass" in rendered

    def test_render_omits_expectation_line_when_blank(self):
        step = Step(
            tool_call_intent="run pytest", rationale="prove green",
            expected_observation="", index=1,
        )
        assert "expect:" not in step.render()


# ── Plan ────────────────────────────────────────────────────────────────────

def _step(intent: str) -> Step:
    return Step(tool_call_intent=intent, rationale="r-" + intent, expected_observation="o-" + intent)


class TestPlan:
    def test_assigns_one_based_indices_in_order(self):
        plan = Plan(steps=[_step("a"), _step("b"), _step("c")], goal="demo")
        assert [s.index for s in plan.steps] == [1, 2, 3]
        assert [s.tool_call_intent for s in plan.steps] == ["a", "b", "c"]

    def test_reindexes_regardless_of_caller_supplied_index(self):
        # Caller-set indices are normalized to canonical 1..N.
        messy = [_step("a").with_index(99), _step("b").with_index(5)]
        plan = Plan(steps=messy)
        assert [s.index for s in plan.steps] == [1, 2]

    def test_is_frozen(self):
        plan = Plan(steps=[_step("a")])
        with pytest.raises((AttributeError, TypeError)):
            plan.goal = "mutated"  # type: ignore[misc]

    def test_empty_plan_rejected(self):
        with pytest.raises(ValueError):
            Plan(steps=[])

    def test_len_reflects_step_count(self):
        assert len(Plan(steps=[_step("a"), _step("b")])) == 2

    def test_metadata_carried_verbatim(self):
        plan = Plan(steps=[_step("a")], metadata={"source": "test", "k": 1})
        assert plan.metadata == {"source": "test", "k": 1}

    def test_round_trip_dict(self):
        plan = Plan(
            steps=[_step("a"), _step("b")],
            goal="ship the schema",
            metadata={"origin": "#290"},
        )
        restored = Plan.from_dict(plan.to_dict())
        assert restored == plan

    def test_render_has_goal_header_and_every_step(self):
        plan = Plan(steps=[_step("alpha"), _step("beta")], goal="do the thing")
        rendered = plan.render()
        assert "Plan — do the thing" in rendered
        assert "1. alpha" in rendered
        assert "2. beta" in rendered

    def test_render_without_goal_uses_bare_header(self):
        rendered = Plan(steps=[_step("only")]).render()
        assert rendered.splitlines()[0] == "Plan"


# ── emit_plan (the inert seam) ───────────────────────────────────────────────

class TestEmitPlan:
    def test_none_plan_is_inert(self):
        sink: list[str] = []
        emitted = emit_plan(None, emit=sink.append)
        assert emitted is False
        assert sink == []  # default-off: nothing rendered, nothing emitted

    def test_emits_rendered_plan_once(self):
        sink: list[str] = []
        plan = Plan(steps=[_step("a")], goal="g")
        emitted = emit_plan(plan, emit=sink.append)
        assert emitted is True
        assert len(sink) == 1
        assert "Plan — g" in sink[0]
        assert "1. a" in sink[0]

    def test_broken_sink_does_not_raise(self):
        def boom(_text: str) -> None:
            raise RuntimeError("sink exploded")

        # Emission must never break a turn — a misbehaving sink is swallowed.
        assert emit_plan(Plan(steps=[_step("a")]), emit=boom) is False
