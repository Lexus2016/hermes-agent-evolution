"""Plan-and-execute opt-in flag + a model-free planner (issue #292).

This is the **third and final** child of the plan-and-execute decomposition
(parent #283). The two prior slices shipped *inert* machinery:

* #290 (:mod:`agent.plan_schema`) — the :class:`~agent.plan_schema.Plan` /
  :class:`~agent.plan_schema.Step` data model and the ``emit_plan`` sink. The
  agent's ``_emit_plan_before_tool_calls`` hook calls it, but is a no-op until
  ``self._active_plan`` is set.
* #291 (:mod:`agent.plan_lookahead`) — ``evaluate_divergence`` / ``PlanProgress``
  / ``trigger_replan``. The agent's ``_check_step_divergence_after_tool_calls``
  hook calls it, but likewise self-gates to a no-op while ``self._active_plan``
  is ``None``.

Both seams exist in ``run_agent.py`` already and do nothing in the default
loop, because *nothing sets an active plan*. This module is the missing
opt-in: a flag reader (default **off**) and a deterministic, model-free planner.
When — and only when — the flag is enabled, the agent builds a plan from the
user's task and assigns it to ``self._active_plan``, which is what flips the two
inert hooks live.

Why default-off matters
------------------------
Constraint from #292: *default agent behavior is byte-identical unless plan_mode
is explicitly enabled*. :func:`plan_mode_enabled` returns ``False`` unless the
operator opts in via the ``HERMES_PLAN_MODE`` env var or the ``plan_mode`` config
key. With the flag off, ``_maybe_activate_plan_mode`` never assigns
``_active_plan``, so the emission and divergence hooks stay exactly as inert as
they were after #290/#291 — no plan is built, no status is emitted, no tool
selection changes.

Why a model-free planner
------------------------
The point of this slice is to *activate the seams* and provide an eval harness
that compares plan-mode-on against the ReAct baseline **without a live model**.
:func:`build_stub_plan` derives a small, deterministic :class:`Plan` from the
task text using only string heuristics — zero tokens, zero latency, fully
reproducible. It is intentionally simple: a real LLM planner is a strict upgrade
that can replace it behind the same ``self._active_plan`` assignment. Keeping it
here (not in ``run_agent.py``) lets the eval harness import and exercise the whole
plan-mode path with no agent instance and no network.
"""

from __future__ import annotations

import os
from typing import Any, Callable, Dict, List, Optional

from agent.plan_schema import Plan, Step
from utils import is_truthy_value

# Env var and config key that turn plan mode on. Both default-off: the flag is
# enabled only when explicitly set to a truthy value.
PLAN_MODE_ENV_VAR = "HERMES_PLAN_MODE"
PLAN_MODE_CONFIG_KEY = "plan_mode"

# A config loader: returns the parsed config dict (``hermes_cli.config.load_config``
# shape). Injectable so the flag reader and its tests never have to touch disk.
ConfigLoader = Callable[[], Dict[str, Any]]


def _default_config_loader() -> Dict[str, Any]:
    """Load persisted config, swallowing any error to an empty dict.

    Imported lazily (like the other flag readers in ``run_agent.py``) so a config
    import problem can never break agent startup — a missing/broken config simply
    means "flag off", the safe default.
    """
    try:
        from hermes_cli.config import load_config as _load_config

        cfg = _load_config()
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def plan_mode_enabled(config_loader: Optional[ConfigLoader] = None) -> bool:
    """Return whether plan-and-execute mode is opted in. **Default: off.**

    Resolution order (first decisive wins), mirroring the established
    ``_file_mutation_verifier_enabled`` / ``_turn_completion_explainer_enabled``
    pattern in ``run_agent.py`` — except the safe default is ``False`` here, not
    ``True``:

    1. ``HERMES_PLAN_MODE`` env var. When present, it decides outright
       (truthy → on, anything else → off). The env var is the operator's
       per-process override.
    2. ``plan_mode`` config key (top level of the persisted config). When the
       env var is unset but the key is present, its truthiness decides.
    3. Otherwise **off** — the byte-identical-to-baseline default.

    ``config_loader`` is injected only by tests; production passes ``None`` and
    gets the lazy disk loader.
    """
    env = os.environ.get(PLAN_MODE_ENV_VAR)
    if env is not None:
        # An explicit env value is decisive in both directions: "0"/"false"/""
        # forces off even if config says on.
        return is_truthy_value(env, default=False)

    loader = config_loader or _default_config_loader
    try:
        cfg = loader() or {}
    except Exception:
        cfg = {}
    if isinstance(cfg, dict) and PLAN_MODE_CONFIG_KEY in cfg:
        return is_truthy_value(cfg.get(PLAN_MODE_CONFIG_KEY), default=False)

    return False  # safe default: plan mode OFF -> seams stay inert


# Heuristic task-type detection for the stub planner. Order matters: the first
# family whose markers hit decides the plan template. Markers are lowercase
# substrings matched against the task text — deliberately coarse, since the stub
# only needs to produce a *plausible, deterministic* step skeleton, not a smart
# one.
_RESEARCH_MARKERS: tuple[str, ...] = (
    "research", "investigate", "compare", "summarize", "find out",
    "look up", "gather", "report on", "survey", "benchmark numbers",
)
_REFACTOR_MARKERS: tuple[str, ...] = (
    "refactor", "rename", "migrate", "extract", "split", "restructure",
    "move the", "across files", "multi-file", "rework",
)


def _task_family(task: str) -> str:
    """Classify a task string as ``"research"``, ``"refactor"``, or ``"generic"``."""
    low = (task or "").lower()
    if any(marker in low for marker in _REFACTOR_MARKERS):
        return "refactor"
    if any(marker in low for marker in _RESEARCH_MARKERS):
        return "research"
    return "generic"


def _trimmed_goal(task: str, *, limit: int = 120) -> str:
    """A one-line goal derived from the task's first line, length-capped."""
    first_line = (task or "").strip().splitlines()[0] if (task or "").strip() else ""
    if len(first_line) > limit:
        return first_line[: limit - 1].rstrip() + "…"
    return first_line


def build_stub_plan(task: str) -> Optional[Plan]:
    """Build a deterministic, model-free :class:`Plan` from a task string.

    Returns ``None`` for an empty/blank task (nothing to plan), so the caller's
    "no plan" branch — the unchanged-behavior path — is taken. Otherwise returns
    a small ordered plan whose shape depends on the detected task family
    (multi-file refactor vs. multi-step research vs. a generic three-step
    skeleton). Every step carries an ``expected_observation`` so the #291
    divergence comparator has a yardstick to judge against.

    This is a *stub*: a real LLM planner is a drop-in replacement that produces
    the same :class:`Plan` type. Keeping the logic pure and deterministic is what
    lets the eval harness run plan-mode end-to-end with no live model.
    """
    if not task or not task.strip():
        return None

    family = _task_family(task)
    goal = _trimmed_goal(task)

    if family == "refactor":
        steps = [
            Step(
                tool_call_intent="map the files and symbols the change touches",
                rationale="a multi-file refactor needs the full blast radius before edits",
                expected_observation="a list of affected files and the symbols to change",
            ),
            Step(
                tool_call_intent="apply the edits file by file, preserving behavior",
                rationale="edit each site once the targets are known to avoid backtracking",
                expected_observation="each target file modified with the renamed/extracted code",
            ),
            Step(
                tool_call_intent="run the test suite to confirm no regression",
                rationale="prove the refactor preserved behavior before reporting done",
                expected_observation="the test suite passes with all tests green",
            ),
        ]
    elif family == "research":
        steps = [
            Step(
                tool_call_intent="gather sources relevant to the question",
                rationale="multi-step research starts by collecting candidate sources",
                expected_observation="a set of relevant sources or search results",
            ),
            Step(
                tool_call_intent="extract the key facts and figures from the sources",
                rationale="distill the raw sources into the specific data the task asks for",
                expected_observation="the extracted facts, numbers, or findings",
            ),
            Step(
                tool_call_intent="synthesize the findings into the requested report",
                rationale="combine the extracted facts into the deliverable the task wants",
                expected_observation="a written synthesis answering the research question",
            ),
        ]
    else:
        steps = [
            Step(
                tool_call_intent="understand the task and the current state",
                rationale="orient before acting so the first action is well-chosen",
                expected_observation="the relevant context for the task",
            ),
            Step(
                tool_call_intent="carry out the core work of the task",
                rationale="execute the main action once context is gathered",
                expected_observation="the task's primary change or output",
            ),
            Step(
                tool_call_intent="verify the result satisfies the task",
                rationale="confirm success before reporting completion",
                expected_observation="evidence the task was completed correctly",
            ),
        ]

    return Plan(steps=steps, goal=goal, metadata={"source": "stub_planner", "family": family})
