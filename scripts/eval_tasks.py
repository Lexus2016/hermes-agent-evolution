#!/usr/bin/env python3
"""Fixed, versioned task set for the eval harness (issue #1514, Step 1).

A small gold-standard eval dataset of deterministic agent tasks sourced from
real evolution-cycle work. Smoke-wired into scripts/evolution_evaluator.py at
import so the loader is exercised in CI (PR #1520 was closed as dead code; the
wire keeps this module live). Pure data + a loader; no agent execution.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

# Bump on a task add/remove/change that invalidates historical scores.
TASK_SET_VERSION = "1.0"


@dataclass(frozen=True)
class EvalTask:
    """One deterministic eval task drawn from real evolution-cycle work.

    id, prompt, expected_tools, expected_result_pattern, category, difficulty
    per the issue's success criteria.
    """

    id: str
    prompt: str
    expected_tools: List[str] = field(default_factory=list)
    expected_result_pattern: Optional[str] = None
    category: str = "reasoning"
    difficulty: str = "easy"


# Canonical v1.0 task set — small, real pieces of evolution work, covering
# >=3 tool categories (search, file-ops, code, reasoning).
_TASKS_V1: List[EvalTask] = [
    EvalTask(
        id="v1-list-python-files",
        prompt="List the Python files under scripts/ whose names start with 'evolution_'.",
        expected_tools=["search_files"],
        expected_result_pattern="evolution_",
        category="search",
        difficulty="easy",
    ),
    EvalTask(
        id="v1-read-evaluator-rubric",
        prompt="Report which criterion in DEFAULT_RUBRIC (scripts/evolution_evaluator.py) has the highest weight.",
        expected_tools=["read_file"],
        expected_result_pattern="correctness",
        category="file-ops",
        difficulty="easy",
    ),
    EvalTask(
        id="v1-parse-evaluator-verdict",
        prompt="Return the three verdict constants from scripts/evolution_evaluator.py as a comma-separated list.",
        expected_tools=["read_file"],
        expected_result_pattern="ACCEPT",
        category="code",
        difficulty="easy",
    ),
    EvalTask(
        id="v1-merge-gate-cap",
        prompt="What is the default max-lines autonomous self-merge cap in scripts/evolution_merge_gate.py? Answer with the integer.",
        expected_tools=["read_file", "search_files"],
        expected_result_pattern="200",
        category="code",
        difficulty="medium",
    ),
    EvalTask(
        id="v1-sum-rubric-weights",
        prompt="Sum the numeric weights in DEFAULT_RUBRIC (scripts/evolution_evaluator.py) and report the total.",
        expected_tools=["read_file"],
        expected_result_pattern="4.0",
        category="code",
        difficulty="medium",
    ),
]


def _by_version(version: str) -> Dict[str, List[EvalTask]]:
    return {"1.0": _TASKS_V1, "1": _TASKS_V1}  # "1" = major-version alias


def latest_version() -> str:
    """Return the highest task-set version string currently defined."""
    return TASK_SET_VERSION


def load_task_set(version: str = TASK_SET_VERSION) -> List[EvalTask]:
    """Load the task list for ``version``. Raises KeyError for an unknown
    version (fail loud — a silent empty list would let an eval score zero tasks)."""
    sets = _by_version(version)
    if version not in sets:
        known = sorted(k for k in sets if "." in k)
        raise KeyError(f"unknown eval task-set version {version!r}; known: {known}")
    return list(sets[version])
