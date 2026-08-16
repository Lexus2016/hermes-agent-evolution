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
    min_turns: int = 0  # >0 flags a long-horizon task (#2530); 0 = no floor


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


# Long-horizon stress bucket (#2530, LongHorizon-Harness): benchmarks rarely
# test behavior after the 50th tool turn, yet long-running work is where
# reliability collapses. Each task is an iterative survey/verify chain over
# the real evolution scripts — each loop iteration structurally needs its own
# tool turns, so an honest solution cannot compress the chain. NOT part of
# ``_TASKS_V1``/``load_task_set``: 50+ turn runs are too expensive for CI.
LONG_HORIZON_MIN_TURNS = 50

_TASKS_LONG_HORIZON: List[EvalTask] = [
    EvalTask(
        id="lh-extract-helpers-loop",
        prompt=(
            "Run an iterative refactor survey over every scripts/evolution_*.py "
            "module: read each fully, list functions longer than 20 lines, then "
            "per function re-read the module, identify one extractable helper, "
            "search the repo for call sites, verify each, and record the plan. "
            "Report per-module planned-extraction counts and the total."
        ),
        expected_tools=["search_files", "read_file"],
        expected_result_pattern="extraction",
        category="long-horizon",
        difficulty="hard",
        min_turns=60,
    ),
    EvalTask(
        id="lh-import-graph-audit",
        prompt=(
            "Audit imports of every scripts/evolution_*.py module: read each, "
            "list repo-local imports, then per import search the repo to locate "
            "the target file and read it to confirm the symbol exists; re-check "
            "failures with an alternate search pattern. Report per-module "
            "import counts and the unresolved list."
        ),
        expected_tools=["search_files", "read_file"],
        expected_result_pattern="unresolved",
        category="long-horizon",
        difficulty="hard",
        min_turns=50,
    ),
    EvalTask(
        id="lh-config-drift-sweep",
        prompt=(
            "Sweep every scripts/evolution_*.py module for config drift: read "
            "each, extract config keys/defaults/thresholds, then per key search "
            "for other users, read each user, and check values agree. Flag "
            "disagreements. Report a per-module drift table and total count."
        ),
        expected_tools=["search_files", "read_file"],
        expected_result_pattern="drift",
        category="long-horizon",
        difficulty="hard",
        min_turns=50,
    ),
]


def load_long_horizon_tasks() -> List[EvalTask]:
    """Load the opt-in long-horizon stress bucket (#2530) — deliberately kept
    out of ``load_task_set`` so the default split is unchanged; enter via
    ``eval_baseline --with-long-horizon`` (manual/cron only)."""
    return list(_TASKS_LONG_HORIZON)


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
