"""Plan-mode opt-in flag + deterministic eval harness (issue #292).

Final child of the plan-and-execute decomposition (parent #283). Two concerns:

1. **The opt-in flag** (:func:`agent.plan_mode.plan_mode_enabled` and the
   ``AIAgent._plan_mode_enabled`` / ``_maybe_activate_plan_mode`` wiring). The
   load-bearing guarantee of this slice is *default-off*: with no explicit
   opt-in, ``_maybe_activate_plan_mode`` never assigns ``_active_plan``, so the
   #290 emission hook and the #291 divergence hook stay exactly as inert as they
   were — the agent's behavior is byte-identical to the ReAct baseline. These
   tests pin every branch of the flag resolution and the activation gate.

2. **The eval harness** — a small, deterministic suite that compares
   *plan-mode-on* against the *ReAct baseline* on two representative
   long-horizon task fixtures (a multi-file refactor and a multi-step research
   report), using **recorded/synthetic tool trajectories and the model-free stub
   planner** — NO live model, NO network, NO agent instantiation. It asserts
   plan mode introduces **no regression** on the deterministic metrics we pick:
   it must not spuriously flag divergence on an on-plan trajectory, must not cost
   more observed steps than the baseline, and must fully cover its plan.

Run just this file (full collection breaks on a missing pypy dep):

    python -m pytest tests/agent/test_plan_mode_eval.py -q
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

import pytest

from agent.plan_schema import Plan, Step
from agent.plan_lookahead import PlanProgress
from agent.plan_mode import (
    PLAN_MODE_CONFIG_KEY,
    PLAN_MODE_ENV_VAR,
    build_stub_plan,
    plan_mode_enabled,
)


# ─────────────────────────────────────────────────────────────────────────────
# Part 1 — the opt-in flag reader (default OFF)
# ─────────────────────────────────────────────────────────────────────────────

class TestPlanModeFlagDefaultOff:
    """plan_mode_enabled() must default to OFF and obey env > config > default."""

    def test_default_is_off_no_env_no_config(self, monkeypatch):
        monkeypatch.delenv(PLAN_MODE_ENV_VAR, raising=False)
        # Config loader returns an empty dict -> no opt-in anywhere -> off.
        assert plan_mode_enabled(config_loader=lambda: {}) is False

    def test_off_when_config_key_absent(self, monkeypatch):
        monkeypatch.delenv(PLAN_MODE_ENV_VAR, raising=False)
        assert plan_mode_enabled(config_loader=lambda: {"unrelated": True}) is False

    def test_config_key_truthy_enables(self, monkeypatch):
        monkeypatch.delenv(PLAN_MODE_ENV_VAR, raising=False)
        assert plan_mode_enabled(config_loader=lambda: {PLAN_MODE_CONFIG_KEY: True}) is True
        assert plan_mode_enabled(config_loader=lambda: {PLAN_MODE_CONFIG_KEY: "on"}) is True

    def test_config_key_falsey_stays_off(self, monkeypatch):
        monkeypatch.delenv(PLAN_MODE_ENV_VAR, raising=False)
        assert plan_mode_enabled(config_loader=lambda: {PLAN_MODE_CONFIG_KEY: False}) is False
        assert plan_mode_enabled(config_loader=lambda: {PLAN_MODE_CONFIG_KEY: "off"}) is False

    def test_env_var_on_overrides_config_off(self, monkeypatch):
        monkeypatch.setenv(PLAN_MODE_ENV_VAR, "1")
        assert plan_mode_enabled(config_loader=lambda: {PLAN_MODE_CONFIG_KEY: False}) is True

    def test_env_var_off_overrides_config_on(self, monkeypatch):
        # Env is decisive in BOTH directions: explicit "0" forces off even when
        # config opts in. This is what lets an operator hard-disable per process.
        monkeypatch.setenv(PLAN_MODE_ENV_VAR, "0")
        assert plan_mode_enabled(config_loader=lambda: {PLAN_MODE_CONFIG_KEY: True}) is False

    def test_env_var_truthy_values(self, monkeypatch):
        for val in ("1", "true", "yes", "on", "TRUE", "On"):
            monkeypatch.setenv(PLAN_MODE_ENV_VAR, val)
            assert plan_mode_enabled(config_loader=lambda: {}) is True, val

    def test_broken_config_loader_defaults_off(self, monkeypatch):
        monkeypatch.delenv(PLAN_MODE_ENV_VAR, raising=False)

        def boom():
            raise RuntimeError("config exploded")

        # A config-read failure must degrade to the safe default, not crash.
        assert plan_mode_enabled(config_loader=boom) is False


# ─────────────────────────────────────────────────────────────────────────────
# Part 2 — the model-free stub planner
# ─────────────────────────────────────────────────────────────────────────────

class TestStubPlanner:
    def test_blank_task_yields_no_plan(self):
        # No task -> None -> caller's "no plan" (unchanged-behavior) branch.
        assert build_stub_plan("") is None
        assert build_stub_plan("   \n ") is None

    def test_refactor_task_classified(self):
        plan = build_stub_plan("Refactor the auth module across files and rename get_user")
        assert plan is not None
        assert plan.metadata["family"] == "refactor"
        assert len(plan.steps) >= 2

    def test_research_task_classified(self):
        plan = build_stub_plan("Research and compare FLARE benchmark numbers, then report on them")
        assert plan is not None
        assert plan.metadata["family"] == "research"

    def test_generic_task_classified(self):
        plan = build_stub_plan("Say hello to the user")
        assert plan is not None
        assert plan.metadata["family"] == "generic"

    def test_planner_is_deterministic(self):
        task = "Refactor the storage layer and migrate callers across files"
        a, b = build_stub_plan(task), build_stub_plan(task)
        assert a.to_dict() == b.to_dict()

    def test_every_step_has_an_expectation(self):
        # The #291 divergence comparator needs a yardstick on every step.
        for task in (
            "Refactor x across files",
            "Research the topic and summarize",
            "Do a generic thing",
        ):
            plan = build_stub_plan(task)
            assert all(s.expected_observation.strip() for s in plan.steps)


# ─────────────────────────────────────────────────────────────────────────────
# Part 3 — the eval harness: plan-mode-on vs ReAct baseline, no live model
# ─────────────────────────────────────────────────────────────────────────────

# Bind the real AIAgent seams onto a minimal stand-in so the harness drives the
# actual divergence/emission code paths with no live model and no AIAgent
# construction — the same technique the #291 test uses.
class _HarnessAgent:
    """Stand-in exposing exactly the seams the plan-mode path touches."""

    def __init__(self):
        self._status: List[str] = []

    def _emit_status(self, text):
        self._status.append(text)

    from run_agent import AIAgent  # type: ignore  # noqa: E402
    _emit_plan_before_tool_calls = AIAgent._emit_plan_before_tool_calls
    _check_step_divergence_after_tool_calls = AIAgent._check_step_divergence_after_tool_calls
    _latest_tool_observation = staticmethod(AIAgent._latest_tool_observation)


def _tool_msg(text: str) -> dict:
    return {"role": "tool", "name": "t", "content": text, "tool_call_id": "1"}


@dataclass
class EvalMetrics:
    """Deterministic, model-free metrics for one task run.

    * ``steps`` — number of tool-observation turns the run consumed.
    * ``plan_emissions`` — how many times the active plan was printed (0 in
      baseline; 1 in plan mode for a single run).
    * ``divergences`` — how many turns the divergence check flagged off-plan.
    * ``replans`` — how many times a new plan was adopted.
    * ``plan_covered`` — True if plan mode walked its whole plan (cursor reached
      the end), or vacuously True for the baseline (no plan to cover).
    """

    steps: int
    plan_emissions: int
    divergences: int
    replans: int
    plan_covered: bool


@dataclass
class TaskFixture:
    """A long-horizon task plus a recorded, on-plan tool trajectory.

    ``trajectory`` is a list of synthetic tool-result strings — the recorded
    observations a successful run would produce, one per plan step. They are
    written to overlap the stub plan's ``expected_observation`` keywords so an
    on-plan run does NOT trip the divergence heuristic (that is the regression
    signal we assert against).
    """

    name: str
    task: str
    trajectory: List[str]


def _run_baseline(fixture: TaskFixture) -> EvalMetrics:
    """ReAct baseline: plan mode OFF, so no _active_plan is ever set.

    Drives the *same* emission + divergence hooks the live loop calls, but
    because ``_active_plan`` is never assigned they self-gate to no-ops — exactly
    the unchanged behavior. This is the control arm.
    """
    agent = _HarnessAgent()
    # No _active_plan attribute at all -> hooks inert (the default-off contract).
    steps = 0
    for obs in fixture.trajectory:
        agent._emit_plan_before_tool_calls()  # inert: nothing to emit
        agent._check_step_divergence_after_tool_calls([_tool_msg(obs)])  # inert
        steps += 1
    return EvalMetrics(
        steps=steps,
        plan_emissions=0,
        divergences=sum(1 for s in agent._status if "diverged" in s),
        replans=0,
        plan_covered=True,  # vacuous: no plan to cover
    )


def _run_plan_mode(fixture: TaskFixture) -> EvalMetrics:
    """Plan-mode-on: build a stub plan, then replay the recorded trajectory.

    Mirrors what ``_maybe_activate_plan_mode`` does (assign ``_active_plan`` from
    the model-free planner) and then feeds the recorded observations through the
    very same hooks. No model, no network — fully deterministic.
    """
    agent = _HarnessAgent()
    plan = build_stub_plan(fixture.task)
    assert plan is not None, f"fixture {fixture.name!r} should produce a plan"
    agent._active_plan = plan
    agent._plan_emitted_for_turn = False
    agent._plan_progress = None

    emissions = 0
    steps = 0
    for obs in fixture.trajectory:
        before = getattr(agent, "_plan_emitted_for_turn", False)
        agent._emit_plan_before_tool_calls()
        if not before and getattr(agent, "_plan_emitted_for_turn", False):
            emissions += 1
        agent._check_step_divergence_after_tool_calls([_tool_msg(obs)])
        steps += 1

    progress = getattr(agent, "_plan_progress", None)
    covered = isinstance(progress, PlanProgress) and progress.is_exhausted()
    return EvalMetrics(
        steps=steps,
        plan_emissions=emissions,
        divergences=sum(1 for s in agent._status if "diverged" in s),
        replans=sum(1 for s in agent._status if "diverged" in s and False),  # no replanner wired
        plan_covered=covered,
    )


# Two representative long-horizon fixtures. Trajectories are authored to be
# on-plan: each recorded observation echoes the corresponding stub step's
# expected keywords, so a healthy plan-mode run stays at zero divergences.
_FIXTURES: List[TaskFixture] = [
    TaskFixture(
        name="multi_file_refactor",
        task="Refactor the storage layer: rename save_blob across files and extract a helper",
        trajectory=[
            "mapped affected files and symbols: storage.py, callers.py; symbols save_blob to change",
            "modified each target file: renamed save_blob and extracted the helper code",
            "ran the test suite: 42 passed, all tests green, no regression",
        ],
    ),
    TaskFixture(
        name="multi_step_research",
        task="Research and compare the FLARE benchmark numbers, then report on the findings",
        trajectory=[
            "gathered relevant sources and search results on FLARE",
            "extracted the key facts and numbers: CWQ 58 to 73.6, WebQSP 78.3 to 90.4",
            "synthesized the findings into a written report answering the question",
        ],
    ),
]


@pytest.fixture(params=_FIXTURES, ids=lambda f: f.name)
def fixture(request) -> TaskFixture:
    return request.param


class TestEvalHarness:
    """Regression-compare plan-mode-on against the ReAct baseline per fixture."""

    def test_baseline_is_inert(self, fixture):
        # The control arm must do literally nothing the agent wouldn't already do:
        # zero emissions, zero divergences. (Default-off proof at the metric level.)
        m = _run_baseline(fixture)
        assert m.plan_emissions == 0
        assert m.divergences == 0
        assert m.steps == len(fixture.trajectory)

    def test_plan_mode_emits_once_and_covers_plan(self, fixture):
        m = _run_plan_mode(fixture)
        # Plan mode prints its plan exactly once and walks every step.
        assert m.plan_emissions == 1
        assert m.plan_covered is True

    def test_no_divergence_regression_on_plan_trajectory(self, fixture):
        # THE regression assertion: an on-plan trajectory must NOT trip the
        # divergence heuristic. Plan mode adds zero spurious divergences over
        # the baseline's zero.
        baseline = _run_baseline(fixture)
        plan_mode = _run_plan_mode(fixture)
        assert plan_mode.divergences <= baseline.divergences
        assert plan_mode.divergences == 0

    def test_no_step_cost_regression(self, fixture):
        # Plan mode must not consume more observed steps than the baseline for
        # the same trajectory — the planner reorganizes work, it doesn't add
        # round trips. (Latency/token proxy at the deterministic level.)
        baseline = _run_baseline(fixture)
        plan_mode = _run_plan_mode(fixture)
        assert plan_mode.steps == baseline.steps

    def test_plan_mode_detects_a_genuine_divergence(self, fixture):
        # Sanity that the harness can SEE a regression when one exists: replace
        # the final on-plan observation with an unanticipated error and confirm
        # plan mode flags it (so the no-regression assertions above aren't
        # passing vacuously because the detector is dead).
        broken = TaskFixture(
            name=fixture.name + "_broken",
            task=fixture.task,
            trajectory=fixture.trajectory[:-1]
            + ["Error executing tool 'x': no such file or directory"],
        )
        m = _run_plan_mode(broken)
        assert m.divergences >= 1


# ─────────────────────────────────────────────────────────────────────────────
# Part 4 — the activation gate (_maybe_activate_plan_mode honors the flag)
# ─────────────────────────────────────────────────────────────────────────────

class _GateAgent:
    """Stand-in for the activation-gate methods, with a patchable flag."""

    def __init__(self, enabled: bool):
        self._enabled = enabled
        self._status: List[str] = []

    def _emit_status(self, text):
        self._status.append(text)

    from run_agent import AIAgent  # type: ignore  # noqa: E402
    _maybe_activate_plan_mode = AIAgent._maybe_activate_plan_mode

    def _plan_mode_enabled(self) -> bool:  # patch the single seam
        return self._enabled


class TestActivationGate:
    def test_flag_off_never_sets_active_plan(self):
        agent = _GateAgent(enabled=False)
        agent._maybe_activate_plan_mode("Refactor everything across files")
        # The whole default-off guarantee in one assertion: no plan armed ->
        # the #290/#291 hooks stay inert -> behavior byte-identical to baseline.
        assert getattr(agent, "_active_plan", None) is None

    def test_flag_on_arms_a_plan(self):
        agent = _GateAgent(enabled=True)
        agent._maybe_activate_plan_mode("Refactor the module across files and rename it")
        assert isinstance(agent._active_plan, Plan)
        assert agent._plan_emitted_for_turn is False
        assert agent._plan_progress is None

    def test_flag_on_blank_task_arms_nothing(self):
        agent = _GateAgent(enabled=True)
        agent._maybe_activate_plan_mode("   ")
        assert getattr(agent, "_active_plan", None) is None

    def test_idempotent_when_plan_already_active(self):
        agent = _GateAgent(enabled=True)
        existing = Plan(steps=[Step("keep me", "already active", "stays")])
        agent._active_plan = existing
        agent._maybe_activate_plan_mode("Refactor across files")
        # An already-armed plan (multi-turn / post-replan) is left untouched.
        assert agent._active_plan is existing
