# -*- coding: utf-8 -*-
"""Task-decoupled planning: sub-goal DAG with scoped contexts (#1138).

Implements Task-Decoupled Planning (TDP, arXiv:2601.07577): decompose a
long-horizon task into a DAG of sub-goals with explicit dependencies, execute
each with a **scoped context** containing only the information relevant to that
sub-goal (its inputs + its dependency sub-goals' outputs), and **confine
replanning** to the failed node so a local failure revises only that sub-goal —
not the whole trajectory.

This is distinct from uniform context compression: TDP *partitions* the context
by sub-task instead of compressing the whole thing. The mechanism here layers on
the existing ``delegate_task`` primitive — each sub-goal is a scoped subagent
execution — so the core agent loop is unchanged; this module owns the DAG model,
scoped-context assembly, and confined replanning.

Config-gated and off by default; a trigger heuristic keeps short tasks on the
linear loop. Pure functions + frozen dataclasses; no import-time side effects;
standard library only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

__all__ = [
    "SubGoal",
    "SubGoalDAG",
    "TaskDecouplingConfig",
    "decompose_task",
    "scoped_context",
    "confined_replan",
    "should_decouple",
    "load_task_decoupling_config",
]


@dataclass(frozen=True)
class SubGoal:
    """One node of a sub-goal DAG."""

    id: str
    description: str
    dependencies: tuple[str, ...] = ()
    inputs: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "description": self.description,
            "dependencies": list(self.dependencies),
            "inputs": list(self.inputs),
        }


class SubGoalDAG:
    """A validated directed acyclic graph of :class:`SubGoal` nodes."""

    def __init__(self, subgoals: Sequence[SubGoal]) -> None:
        self._nodes: dict[str, SubGoal] = {}
        for sg in subgoals:
            if sg.id in self._nodes:
                raise ValueError(f"duplicate sub-goal id: {sg.id}")
            self._nodes[sg.id] = sg
        self._validate()

    @property
    def nodes(self) -> dict[str, SubGoal]:
        return dict(self._nodes)

    def get(self, goal_id: str) -> SubGoal:
        return self._nodes[goal_id]

    def _validate(self) -> None:
        # Every dependency must reference an existing node.
        for sg in self._nodes.values():
            for dep in sg.dependencies:
                if dep not in self._nodes:
                    raise ValueError(f"sub-goal {sg.id!r} depends on unknown node {dep!r}")
        # Acyclicity via a topological sort (raises on cycle).
        self.topological_order()

    def topological_order(self) -> list[str]:
        """Return node ids in dependency order (raises ``ValueError`` on a cycle)."""
        indeg = {nid: 0 for nid in self._nodes}
        for sg in self._nodes.values():
            for _dep in sg.dependencies:
                indeg[sg.id] += 1
        # Deterministic: process ready nodes in insertion order.
        ready = [nid for nid in self._nodes if indeg[nid] == 0]
        order: list[str] = []
        while ready:
            nid = ready.pop(0)
            order.append(nid)
            for other in self._nodes.values():
                if nid in other.dependencies:
                    indeg[other.id] -= 1
                    if indeg[other.id] == 0:
                        ready.append(other.id)
        if len(order) != len(self._nodes):
            raise ValueError("sub-goal DAG contains a cycle")
        return order

    def ancestors(self, goal_id: str) -> set[str]:
        """Return all transitive dependency ids of ``goal_id``."""
        seen: set[str] = set()
        stack = list(self._nodes[goal_id].dependencies)
        while stack:
            nid = stack.pop()
            if nid in seen:
                continue
            seen.add(nid)
            stack.extend(self._nodes[nid].dependencies)
        return seen

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodes": [self._nodes[nid].to_dict() for nid in self.topological_order()],
        }


@dataclass(frozen=True)
class TaskDecouplingConfig:
    """Config for the ``task_decoupling`` section (off by default)."""

    enabled: bool = False
    min_task_chars: int = 280  # trigger heuristic: only long tasks decouple

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "TaskDecouplingConfig":
        if not isinstance(data, Mapping):
            return cls()
        defaults = cls()
        try:
            min_chars = int(data.get("min_task_chars", defaults.min_task_chars))
        except (TypeError, ValueError):
            min_chars = defaults.min_task_chars
        return cls(
            enabled=bool(data.get("enabled", defaults.enabled)),
            min_task_chars=max(1, min_chars),
        )


def load_task_decoupling_config() -> TaskDecouplingConfig:
    """Lazily load the ``task_decoupling`` config section (safe default: off).

    Uses ``load_config_readonly`` (no defensive deepcopy) because this runs once
    per turn in the conversation loop and only reads the config.
    """
    try:
        try:
            from hermes_cli.config import load_config_readonly as _load
        except ImportError:
            from hermes_cli.config import load_config as _load

        cfg = _load()
        if isinstance(cfg, Mapping):
            return TaskDecouplingConfig.from_mapping(cfg.get("task_decoupling", {}))
    except Exception:
        pass
    return TaskDecouplingConfig()


def should_decouple(task: str, config: TaskDecouplingConfig | None = None) -> bool:
    """Trigger heuristic: long tasks decouple, short ones stay on the linear loop."""
    cfg = config or TaskDecouplingConfig()
    if not cfg.enabled:
        return False
    return len(task or "") >= cfg.min_task_chars


_STEP_SPLIT_RE = re.compile(r"(?:^|\n)\s*(?:\d+[.)]|[-*])\s+")


def _heuristic_split(task: str) -> list[str]:
    """Deterministic fallback decomposition: numbered/bulleted lines, else clauses."""
    parts = [p.strip() for p in _STEP_SPLIT_RE.split(task) if p and p.strip()]
    if len(parts) >= 2:
        return parts
    # Fall back to splitting on "; then " / " and then " / newlines.
    parts = [p.strip() for p in re.split(r";|\bthen\b|\n", task) if p and p.strip()]
    return parts if len(parts) >= 2 else [task.strip()]


def decompose_task(
    task: str,
    *,
    decomposer: Callable[[str], Sequence[SubGoal]] | None = None,
    linear_dependencies: bool = True,
) -> SubGoalDAG:
    """Decompose ``task`` into a :class:`SubGoalDAG`.

    ``decomposer`` (injectable — an LLM-backed splitter in production) takes the
    task and returns explicit sub-goals with dependencies. When omitted, a
    deterministic heuristic splits the task into ordered sub-goals; with
    ``linear_dependencies`` each depends on the previous (a chain), which is the
    safe default for an unknown structure.
    """
    if decomposer is not None:
        return SubGoalDAG(list(decomposer(task)))
    pieces = _heuristic_split(task)
    subgoals: list[SubGoal] = []
    for i, piece in enumerate(pieces):
        deps = (f"g{i - 1}",) if (linear_dependencies and i > 0) else ()
        subgoals.append(SubGoal(id=f"g{i}", description=piece, dependencies=deps))
    return SubGoalDAG(subgoals)


def scoped_context(
    dag: SubGoalDAG,
    goal_id: str,
    *,
    dependency_outputs: Mapping[str, Any] | None = None,
    base_inputs: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble the scoped context for ``goal_id``.

    Includes ONLY: the sub-goal's own declared inputs, and the outputs of its
    (transitive) dependency sub-goals. Outputs of unrelated sub-goals are
    excluded — that partitioning is the whole point of TDP. Returns a plain dict
    suitable for handing to a scoped subagent.
    """
    sg = dag.get(goal_id)
    deps = dag.ancestors(goal_id)
    outputs = dependency_outputs or {}
    scoped_outputs = {nid: outputs[nid] for nid in deps if nid in outputs}
    inputs = base_inputs or {}
    scoped_inputs = {k: inputs[k] for k in sg.inputs if k in inputs}
    return {
        "goal_id": goal_id,
        "goal": sg.description,
        "inputs": scoped_inputs,
        "dependency_outputs": scoped_outputs,
    }


def confined_replan(
    dag: SubGoalDAG,
    goal_id: str,
    *,
    revised_description: str | None = None,
    replanner: Callable[[SubGoal], SubGoal] | None = None,
) -> SubGoalDAG:
    """Return a NEW DAG with only ``goal_id`` revised; all other nodes intact.

    Either supply ``revised_description`` or a ``replanner`` that maps the failed
    sub-goal to a revised one (keeping the same id + dependencies so the DAG
    shape is preserved). Sibling and ancestor nodes are copied unchanged — a
    local failure never mutates the rest of the trajectory.
    """
    old = dag.get(goal_id)
    if replanner is not None:
        revised = replanner(old)
        if revised.id != old.id or set(revised.dependencies) != set(old.dependencies):
            raise ValueError("confined replan must preserve the node id and dependencies")
    else:
        revised = SubGoal(
            id=old.id,
            description=revised_description if revised_description is not None else old.description,
            dependencies=old.dependencies,
            inputs=old.inputs,
        )
    new_nodes = [revised if nid == goal_id else dag.get(nid) for nid in dag.topological_order()]
    return SubGoalDAG(new_nodes)
