# -*- coding: utf-8 -*-
"""Plan feasibility validation for the plan-execution loop (#1032, child of #1021).

Second slice of PIVOT-style plan-feasibility validation. The first slice
(``evolution/lib/feasibility_checker.py``) provided standalone check primitives;
this module is the **production, importable** gate that validates a
:class:`agent.plan_schema.Plan` *before* its steps drive any tool calls and
returns structured, per-step feedback for plan revision.

Plan steps are declarative free text (``Step.tool_call_intent`` — e.g.
``"read_file(agent/plan_schema.py)"`` or ``"search the web for X"``), so this
gate is deliberately **conservative**: it only reports :class:`FeasibilityStatus`
``INFEASIBLE`` when it can confidently parse a concrete precondition that is
definitively violated (a referenced file that does not exist, a referenced tool
that is not available). Everything it cannot parse is ``UNCERTAIN`` — never a
blocker on its own — so a false positive can never wrongly halt a plan.

Design goals (matching the surrounding ``agent/`` corpus):

* Pure functions + frozen dataclasses; **no side effects on import**.
* Injectable IO seams (``file_exists``, ``available_tools``) so tests never
  touch disk. Standard library only; full type hints.
* JSON serialisation on every dataclass for storage on ``Plan.metadata``.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Iterable

from agent.plan_schema import Plan, Step

__all__ = [
    "FeasibilityStatus",
    "StepFeasibility",
    "PlanFeasibilityReport",
    "check_plan_feasibility",
    "maybe_validate_plan",
    "PLAN_FEASIBILITY_METADATA_KEY",
]

PLAN_FEASIBILITY_METADATA_KEY = "feasibility"


class FeasibilityStatus(str, Enum):
    """Verdict for a single plan step's preconditions."""

    FEASIBLE = "feasible"
    INFEASIBLE = "infeasible"  # a definitively violated precondition (a blocker)
    UNCERTAIN = "uncertain"  # nothing checkable / could not decide (not a blocker)


# Intents whose first argument is a path that must ALREADY EXIST to proceed.
_READ_LIKE_TOOLS = frozenset(
    {"read_file", "edit_file", "patch", "apply_patch", "cat", "open"}
)
# Intents whose first argument is a path being WRITTEN — its parent must exist.
_WRITE_LIKE_TOOLS = frozenset({"write_file", "create_file"})

# Leading ``tool_name(args)`` form. Free text without this shape is UNCERTAIN.
_INTENT_CALL_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*\((.*)\)\s*$", re.DOTALL)


@dataclass(frozen=True)
class StepFeasibility:
    """Feasibility verdict for one plan step."""

    index: int
    intent: str
    status: FeasibilityStatus
    reason: str = ""
    suggestion: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_blocker(self) -> bool:
        return self.status is FeasibilityStatus.INFEASIBLE

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "intent": self.intent,
            "status": self.status.value,
            "reason": self.reason,
            "suggestion": self.suggestion,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class PlanFeasibilityReport:
    """Aggregate feasibility of a whole plan."""

    steps: tuple[StepFeasibility, ...] = ()

    @property
    def feasible(self) -> bool:
        """True unless at least one step is a definitive blocker."""
        return not any(s.is_blocker for s in self.steps)

    @property
    def blockers(self) -> list[StepFeasibility]:
        return [s for s in self.steps if s.is_blocker]

    def to_dict(self) -> dict[str, Any]:
        return {
            "feasible": self.feasible,
            "blocker_count": len(self.blockers),
            "steps": [s.to_dict() for s in self.steps],
        }


def _default_file_exists(path: str) -> bool:
    return os.path.isfile(path)


def _first_arg_path(raw_args: str) -> str | None:
    """Extract the first positional argument as a path, stripped of quotes."""
    if not raw_args.strip():
        return None
    first = raw_args.split(",", 1)[0].strip()
    if not first:
        return None
    # Drop keyword-arg forms like ``path=...`` -> keep the value.
    if "=" in first and not first.startswith(("'", '"', "/", ".")):
        first = first.split("=", 1)[1].strip()
    first = first.strip("'\"").strip()
    return first or None


def _looks_like_path(candidate: str) -> bool:
    """Heuristic: only treat a token as a checkable path when it plausibly is one."""
    if not candidate or " " in candidate:
        return False
    return "/" in candidate or "." in candidate


def _check_step(
    index: int,
    step: Step,
    *,
    file_exists: Callable[[str], bool],
    available_tools: frozenset[str] | None,
) -> StepFeasibility:
    intent = step.tool_call_intent or ""
    match = _INTENT_CALL_RE.match(intent)
    if not match:
        return StepFeasibility(index, intent, FeasibilityStatus.UNCERTAIN)

    tool = match.group(1).strip().lower()
    raw_args = match.group(2)

    # Tool availability — only when the caller supplied the known tool set.
    if available_tools is not None and tool not in available_tools:
        return StepFeasibility(
            index,
            intent,
            FeasibilityStatus.INFEASIBLE,
            reason=f"tool '{tool}' is not available",
            suggestion="use an available tool or drop this step",
            metadata={"tool": tool},
        )

    if tool in _READ_LIKE_TOOLS:
        path = _first_arg_path(raw_args)
        if path and _looks_like_path(path):
            if not file_exists(path):
                return StepFeasibility(
                    index,
                    intent,
                    FeasibilityStatus.INFEASIBLE,
                    reason=f"target file does not exist: {path}",
                    suggestion="verify the path (search or list the directory) before this step",
                    metadata={"path": path, "tool": tool},
                )
            return StepFeasibility(index, intent, FeasibilityStatus.FEASIBLE, metadata={"path": path})

    if tool in _WRITE_LIKE_TOOLS:
        path = _first_arg_path(raw_args)
        if path and _looks_like_path(path):
            parent = os.path.dirname(path) or "."
            # Only a blocker when the parent directory is definitively missing.
            if not os.path.isdir(parent) and not file_exists(path):
                return StepFeasibility(
                    index,
                    intent,
                    FeasibilityStatus.UNCERTAIN,
                    reason=f"parent directory may not exist: {parent}",
                    suggestion="create the parent directory before writing",
                    metadata={"path": path, "parent": parent, "tool": tool},
                )
            return StepFeasibility(index, intent, FeasibilityStatus.FEASIBLE, metadata={"path": path})

    return StepFeasibility(index, intent, FeasibilityStatus.UNCERTAIN, metadata={"tool": tool})


def check_plan_feasibility(
    plan: Plan,
    *,
    file_exists: Callable[[str], bool] | None = None,
    available_tools: Iterable[str] | None = None,
) -> PlanFeasibilityReport:
    """Validate every step of ``plan`` and return a structured report.

    ``file_exists`` and ``available_tools`` are injectable so tests never touch
    the disk or the live tool registry. When ``available_tools`` is ``None`` the
    tool-availability check is skipped entirely (conservative default).
    """
    fx = file_exists or _default_file_exists
    tools = frozenset(t.strip().lower() for t in available_tools) if available_tools is not None else None
    results = tuple(
        _check_step(i, step, file_exists=fx, available_tools=tools)
        for i, step in enumerate(plan.steps, start=1)
    )
    return PlanFeasibilityReport(steps=results)


def maybe_validate_plan(
    plan: Plan | None,
    *,
    enabled: bool,
    file_exists: Callable[[str], bool] | None = None,
    available_tools: Iterable[str] | None = None,
) -> PlanFeasibilityReport | None:
    """Runtime seam: validate ``plan`` and record the report on its metadata.

    Returns ``None`` — and touches nothing — unless ``enabled`` is true and a
    plan is supplied, so the default (disabled) path is a pure no-op. On success
    the report is stored under ``plan.metadata['feasibility']`` (the ``metadata``
    dict is mutable even though ``Plan`` is frozen) for a later revision pass to
    consume. Never raises — a validation error degrades to ``None``.
    """
    if not enabled or plan is None:
        return None
    try:
        report = check_plan_feasibility(
            plan, file_exists=file_exists, available_tools=available_tools
        )
        if isinstance(plan.metadata, dict):
            plan.metadata[PLAN_FEASIBILITY_METADATA_KEY] = report.to_dict()
        return report
    except Exception:
        return None
