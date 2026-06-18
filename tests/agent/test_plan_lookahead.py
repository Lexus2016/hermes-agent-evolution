"""Tests for agent.plan_lookahead — divergence detection + replan trigger (#291).

Covers child #2 of the plan-and-execute decomposition (parent #283):

* the pure :func:`evaluate_divergence` comparator and its signal taxonomy,
* the :class:`PlanProgress` cursor,
* the default-off :func:`should_replan` / :func:`trigger_replan` seam
  (inert unless a ``replanner`` is wired — sibling #292 wires it),
* and the run_agent.py integration: the seam self-gates to a no-op until an
  active plan is set, mirroring the #290 emission hook.
"""

import pytest

from agent.plan_schema import Plan, Step
from agent.plan_lookahead import (
    DivergenceResult,
    PlanProgress,
    Replanner,
    evaluate_divergence,
    should_replan,
    trigger_replan,
    SIGNAL_OK,
    SIGNAL_NO_EXPECTATION,
    SIGNAL_EMPTY_OBSERVATION,
    SIGNAL_ERROR,
    SIGNAL_MISMATCH,
)


def _step(intent="run pytest", rationale="prove green", expect="all tests pass"):
    return Step(
        tool_call_intent=intent,
        rationale=rationale,
        expected_observation=expect,
    )


# ── evaluate_divergence (the pure comparator) ────────────────────────────────

class TestEvaluateDivergence:
    def test_no_expectation_never_diverges(self):
        # A step that predicts nothing has no yardstick — can't diverge.
        step = _step(expect="")
        result = evaluate_divergence("anything at all here", step)
        assert result.diverged is False
        assert result.signal == SIGNAL_NO_EXPECTATION

    def test_whitespace_only_expectation_never_diverges(self):
        result = evaluate_divergence("anything", _step(expect="   \n  "))
        assert result.diverged is False
        assert result.signal == SIGNAL_NO_EXPECTATION

    def test_empty_observation_when_something_expected_diverges(self):
        result = evaluate_divergence("", _step(expect="the test suite passes"))
        assert result.diverged is True
        assert result.signal == SIGNAL_EMPTY_OBSERVATION

    def test_none_observation_treated_as_empty(self):
        result = evaluate_divergence(None, _step(expect="some output expected"))
        assert result.diverged is True
        assert result.signal == SIGNAL_EMPTY_OBSERVATION

    def test_unanticipated_error_diverges(self):
        step = _step(expect="the file contents of plan_schema.py")
        observed = "Error executing tool 'read_file': no such file or directory"
        result = evaluate_divergence(observed, step)
        assert result.diverged is True
        assert result.signal == SIGNAL_ERROR

    def test_anticipated_error_does_not_diverge_on_error(self):
        # A step whose expectation itself describes an error must not be flagged
        # just for observing one — it falls through to keyword matching.
        step = _step(expect="a traceback (most recent call last) from the failing import")
        observed = "Traceback (most recent call last):\n  import boom\nImportError"
        result = evaluate_divergence(observed, step)
        # Error marker present in BOTH expectation and observation -> not an
        # error-signal divergence; keyword overlap (traceback/import) keeps it on-plan.
        assert result.signal != SIGNAL_ERROR
        assert result.diverged is False

    def test_matching_keywords_on_plan(self):
        step = _step(expect="the pytest suite passes with all green")
        observed = "collected 12 items ... 12 passed. pytest suite green."
        result = evaluate_divergence(observed, step)
        assert result.diverged is False
        assert result.signal == SIGNAL_OK
        assert result.overlap > 0.0

    def test_keyword_mismatch_diverges(self):
        step = _step(expect="a JSON list of three FLARE benchmark numbers")
        observed = "the weather in Paris is sunny and warm today"
        result = evaluate_divergence(observed, step)
        assert result.diverged is True
        assert result.signal == SIGNAL_MISMATCH
        assert 0.0 <= result.overlap < 0.34

    def test_threshold_is_tunable(self):
        step = _step(expect="alpha beta gamma delta")  # 4 keywords
        observed = "alpha only"  # 1/4 = 0.25 overlap
        # Default threshold (0.34) -> diverged.
        assert evaluate_divergence(observed, step).diverged is True
        # Lenient threshold below the overlap -> on plan.
        assert evaluate_divergence(observed, step, overlap_threshold=0.2).diverged is False

    def test_expectation_all_stopwords_is_not_testable(self):
        # No meaningful keywords -> treated as on-plan, not a false mismatch.
        result = evaluate_divergence("zzz qqq", _step(expect="the it is to be"))
        assert result.diverged is False
        assert result.signal == SIGNAL_NO_EXPECTATION

    def test_result_is_frozen(self):
        result = evaluate_divergence("x", _step(expect=""))
        with pytest.raises((AttributeError, TypeError)):
            result.diverged = True  # type: ignore[misc]


# ── PlanProgress (the execution cursor) ──────────────────────────────────────

class TestPlanProgress:
    def _plan(self, n=3):
        return Plan(steps=[_step(intent=f"s{i}") for i in range(n)])

    def test_current_points_at_first_step_initially(self):
        prog = PlanProgress(self._plan())
        assert prog.current().index == 1

    def test_advance_walks_steps_then_exhausts(self):
        plan = self._plan(2)
        prog = PlanProgress(plan)
        assert prog.current().index == 1
        prog.advance()
        assert prog.current().index == 2
        prog.advance()
        assert prog.current() is None
        assert prog.is_exhausted() is True

    def test_advance_clamps_past_end(self):
        prog = PlanProgress(self._plan(1))
        prog.advance()
        prog.advance()  # extra advance must not run the cursor away
        assert prog.cursor == 1
        assert prog.is_exhausted() is True

    def test_adopt_swaps_plan_and_resets_cursor(self):
        prog = PlanProgress(self._plan(2))
        prog.advance()
        assert prog.cursor == 1
        new_plan = self._plan(3)
        prog.adopt(new_plan)
        assert prog.plan is new_plan
        assert prog.cursor == 0
        assert prog.current().index == 1


# ── should_replan / trigger_replan (the default-off seam) ────────────────────

class TestReplanSeam:
    def test_should_replan_tracks_divergence(self):
        assert should_replan(DivergenceResult(True, "r", SIGNAL_MISMATCH)) is True
        assert should_replan(DivergenceResult(False, "r", SIGNAL_OK)) is False

    def test_trigger_replan_inert_without_replanner(self):
        # Default-off: no replanner wired -> no new plan, current plan stands.
        plan = Plan(steps=[_step()])
        result = DivergenceResult(True, "diverged", SIGNAL_MISMATCH)
        assert trigger_replan(plan, plan.steps[0], "obs", result) is None

    def test_trigger_replan_invokes_wired_replanner(self):
        plan = Plan(steps=[_step(intent="old")])
        fresh = Plan(steps=[_step(intent="new")])
        seen = {}

        def replanner(p, s, observed, res):
            seen["args"] = (p, s, observed, res)
            return fresh

        out = trigger_replan(plan, plan.steps[0], "obs-text", DivergenceResult(True, "r", SIGNAL_ERROR), replanner=replanner)
        assert out is fresh
        assert seen["args"][0] is plan
        assert seen["args"][2] == "obs-text"

    def test_trigger_replan_swallows_replanner_exception(self):
        def boom(*_args):
            raise RuntimeError("replanner exploded")

        plan = Plan(steps=[_step()])
        # A misbehaving replanner must never break a turn.
        out = trigger_replan(plan, plan.steps[0], "o", DivergenceResult(True, "r", SIGNAL_ERROR), replanner=boom)
        assert out is None

    def test_trigger_replan_ignores_non_plan_return(self):
        def bad(*_args):
            return "not a plan"

        plan = Plan(steps=[_step()])
        assert trigger_replan(plan, plan.steps[0], "o", DivergenceResult(True, "r", SIGNAL_ERROR), replanner=bad) is None

    def test_replanner_alias_is_exported(self):
        # The type alias is part of the seam's public contract (#292 imports it).
        assert Replanner is not None


# ── run_agent.py integration (self-gating default-off) ───────────────────────

class _FakeAgent:
    """Minimal stand-in exposing just the methods the seam touches."""

    def __init__(self):
        self._status: list[str] = []

    def _emit_status(self, text):
        self._status.append(text)

    # Bind the real implementations under test. ``_latest_tool_observation`` is
    # a @staticmethod on AIAgent; re-wrap it so the descriptor survives binding
    # onto this stand-in class (a bare function assignment would turn it into an
    # instance method and inject ``self``).
    from run_agent import AIAgent  # type: ignore  # noqa: E402
    _check_step_divergence_after_tool_calls = AIAgent._check_step_divergence_after_tool_calls
    _latest_tool_observation = staticmethod(AIAgent._latest_tool_observation)


def _tool_msg(text):
    return {"role": "tool", "name": "t", "content": text, "tool_call_id": "1"}


class TestRunAgentSeam:
    def test_no_active_plan_is_inert(self):
        agent = _FakeAgent()
        # No _active_plan attribute at all -> must no-op without raising.
        agent._check_step_divergence_after_tool_calls([_tool_msg("anything")])
        assert agent._status == []
        assert getattr(agent, "_plan_progress", None) is None

    def test_active_plan_on_plan_advances_quietly(self):
        agent = _FakeAgent()
        agent._active_plan = Plan(steps=[_step(expect="all tests pass")])
        agent._plan_progress = None
        agent._check_step_divergence_after_tool_calls([_tool_msg("12 passed, all tests pass, green")])
        # On-plan: no divergence status emitted, cursor advanced.
        assert agent._status == []
        assert agent._plan_progress.cursor == 1

    def test_divergence_emits_status_and_advances_without_replanner(self):
        agent = _FakeAgent()
        agent._active_plan = Plan(steps=[_step(expect="a sorted list of user records")])
        agent._plan_progress = None
        agent._check_step_divergence_after_tool_calls([_tool_msg("Error executing tool 'x': boom")])
        assert any("diverged" in s for s in agent._status)
        # No replanner wired -> plan unchanged, cursor still advances.
        assert agent._plan_progress.cursor == 1
        assert len(agent._active_plan.steps) == 1

    def test_divergence_with_replanner_adopts_new_plan(self):
        agent = _FakeAgent()
        original = Plan(steps=[_step(intent="old", expect="a sorted list of user records")])
        replacement = Plan(steps=[_step(intent="new-a"), _step(intent="new-b")])
        agent._active_plan = original
        agent._plan_progress = None
        agent._replanner = lambda p, s, observed, res: replacement
        agent._plan_emitted_for_turn = True  # pretend already emitted this turn
        agent._check_step_divergence_after_tool_calls([_tool_msg("Error executing tool: nope")])
        # Replanner fired -> new plan adopted, emission re-armed, cursor reset.
        assert agent._active_plan is replacement
        assert agent._plan_emitted_for_turn is False
        assert agent._plan_progress.plan is replacement
        assert agent._plan_progress.cursor == 0

    def test_latest_tool_observation_reads_trailing_tool_messages(self):
        msgs = [
            {"role": "assistant", "content": "calling"},
            _tool_msg("first result"),
            _tool_msg("second result"),
        ]
        obs = _FakeAgent._latest_tool_observation(msgs)
        assert "first result" in obs and "second result" in obs

    def test_latest_tool_observation_none_when_no_trailing_tool(self):
        msgs = [{"role": "assistant", "content": "no tools here"}]
        assert _FakeAgent._latest_tool_observation(msgs) is None

    def test_latest_tool_observation_handles_multimodal_content(self):
        msgs = [{"role": "tool", "name": "t", "tool_call_id": "1",
                 "content": [{"type": "text", "text": "vision text"}, {"type": "image_url"}]}]
        assert _FakeAgent._latest_tool_observation(msgs) == "vision text"
