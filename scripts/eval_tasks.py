#!/usr/bin/env python3
"""Evaluation task-set schema and loader for the Hermes eval harness (#1514).

Defines ``EvalTask`` — a single evaluation scenario with a prompt, expected
tool usage, optional result pattern, category, and difficulty — plus a
versioned task-set registry and a ``load_task_set()`` loader.

This is Step 1 of the eval harness (issue #1514, parent #1481). It provides
only the data layer: the schema, a curated set of 8 tasks across 4 tool
categories, and a loader. The runner (Step 2, #1515) and the null-agent
baseline + scoring (Steps 3-4, #1516) are separate issues.

Design decisions:
- **Pure data, no network.** Tasks are static definitions; the runner
  (Step 2) is responsible for executing them. This keeps the module
  import-safe and unit-testable with no external dependencies.
- **Versioned.** Each task set has a ``version`` string. The loader
  ``load_task_set(version=...)`` returns tasks for the requested version,
  defaulting to the latest. Breaking schema changes bump the major
  version; additive changes bump the minor.
- **≥3 tool categories.** The v1.0 set covers ``code``, ``search``,
  ``file_ops``, and ``reasoning`` — 4 categories, satisfying the ≥3
  requirement.
- **Difficulty levels.** ``easy``, ``medium``, ``hard`` let the runner
  compute per-difficulty pass rates.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Pattern


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EvalTask:
    """A single evaluation task for the Hermes eval harness.

    Attributes:
        id: Unique task identifier (e.g. ``"code-001"``).
        prompt: The user prompt presented to the agent.
        expected_tools: List of tool names the agent is expected to call
            (e.g. ``["terminal", "read_file"]``). Order is not significant.
        expected_result_pattern: Optional regex pattern that the agent's
            final response should match. ``None`` means no pattern check.
        category: Tool category — one of ``"code"``, ``"search"``,
            ``"file_ops"``, ``"reasoning"``.
        difficulty: ``"easy"``, ``"medium"``, or ``"hard"``.
        version: Task-set version this task belongs to (e.g. ``"1.0"``).
        notes: Optional evaluator notes (not shown to the agent).
    """

    id: str
    prompt: str
    expected_tools: List[str] = field(default_factory=list)
    expected_result_pattern: Optional[str] = None
    category: str = "reasoning"
    difficulty: str = "medium"
    version: str = "1.0"
    notes: str = ""

    def compiled_pattern(self) -> Optional[Pattern[str]]:
        """Return the compiled regex for ``expected_result_pattern``, or None."""
        if self.expected_result_pattern is None:
            return None
        return re.compile(self.expected_result_pattern, re.IGNORECASE)


# ---------------------------------------------------------------------------
# Task-set registry
# ---------------------------------------------------------------------------

# v1.0 — initial task set: 8 tasks across 4 categories.
_TASK_SET_V1: List[EvalTask] = [
    # --- Code (2 tasks) ---
    EvalTask(
        id="code-001",
        prompt="Write a Python function that reverses a string. "
               "Put the code in a file called reverse_string.py.",
        expected_tools=["terminal", "read_file"],
        expected_result_pattern=r"def\s+reverse",
        category="code",
        difficulty="easy",
        version="1.0",
        notes="Tests basic code generation + file creation.",
    ),
    EvalTask(
        id="code-002",
        prompt="Write a Python script that reads a CSV file and prints "
               "the number of rows. The script should handle missing files "
               "gracefully. Save it as count_rows.py.",
        expected_tools=["terminal", "read_file"],
        expected_result_pattern=r"csv|pandas|DictReader",
        category="code",
        difficulty="medium",
        version="1.0",
        notes="Tests error handling + file I/O.",
    ),
    # --- Search (2 tasks) ---
    EvalTask(
        id="search-001",
        prompt="Search the web for the latest version of Python and tell "
               "me the version number.",
        expected_tools=["web_search"],
        expected_result_pattern=r"3\.\d+\.\d+",
        category="search",
        difficulty="easy",
        version="1.0",
        notes="Tests web search + information extraction.",
    ),
    EvalTask(
        id="search-002",
        prompt="Find all Python files in the current directory that "
               "contain the word 'logger' and list their paths.",
        expected_tools=["search_files"],
        expected_result_pattern=r"\.py",
        category="search",
        difficulty="easy",
        version="1.0",
        notes="Tests local file search.",
    ),
    # --- File ops (2 tasks) ---
    EvalTask(
        id="file-ops-001",
        prompt="Create a directory called 'test_output' and write a file "
               "named 'hello.txt' inside it with the content 'Hello, World!'.",
        expected_tools=["terminal"],
        expected_result_pattern=r"hello\.txt|Hello.*World",
        category="file_ops",
        difficulty="easy",
        version="1.0",
        notes="Tests directory creation + file writing.",
    ),
    EvalTask(
        id="file-ops-002",
        prompt="Read the file /etc/hostname and tell me what it contains. "
               "If the file doesn't exist, say so.",
        expected_tools=["read_file"],
        expected_result_pattern=r".+",
        category="file_ops",
        difficulty="easy",
        version="1.0",
        notes="Tests file reading + error handling.",
    ),
    # --- Reasoning (2 tasks) ---
    EvalTask(
        id="reasoning-001",
        prompt="If I have 3 apples and give away 1.5, how many do I have left? "
               "Explain your reasoning.",
        expected_tools=[],
        expected_result_pattern=r"1\.5",
        category="reasoning",
        difficulty="easy",
        version="1.0",
        notes="Tests basic arithmetic reasoning. No tools expected.",
    ),
    EvalTask(
        id="reasoning-002",
        prompt="A train travels 60 mph for 2 hours, then 80 mph for 1.5 hours. "
               "What is the total distance traveled? Show your work.",
        expected_tools=[],
        expected_result_pattern=r"240",
        category="reasoning",
        difficulty="medium",
        version="1.0",
        notes="Tests multi-step arithmetic. No tools expected.",
    ),
]

# Registry of all task-set versions.
_TASK_SETS: Dict[str, List[EvalTask]] = {
    "1.0": _TASK_SET_V1,
}

# Latest version (used by load_task_set when version=None).
_LATEST_VERSION = "1.0"


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def load_task_set(version: Optional[str] = None) -> List[EvalTask]:
    """Load a task set by version.

    Args:
        version: Task-set version string (e.g. ``"1.0"``). If ``None``,
            returns the latest version.

    Returns:
        List of ``EvalTask`` objects for the requested version.

    Raises:
        ValueError: If the requested version does not exist.
    """
    if version is None:
        version = _LATEST_VERSION
    if version not in _TASK_SETS:
        available = ", ".join(sorted(_TASK_SETS.keys()))
        raise ValueError(
            f"Unknown task-set version: {version!r}. "
            f"Available versions: {available}"
        )
    return list(_TASK_SETS[version])


def list_versions() -> List[str]:
    """Return all available task-set versions, sorted."""
    return sorted(_TASK_SETS.keys())


def latest_version() -> str:
    """Return the latest task-set version string."""
    return _LATEST_VERSION


def task_count(version: Optional[str] = None) -> int:
    """Return the number of tasks in a task set."""
    return len(load_task_set(version))


def categories(version: Optional[str] = None) -> List[str]:
    """Return the distinct categories in a task set, sorted."""
    return sorted({t.category for t in load_task_set(version)})


def difficulties(version: Optional[str] = None) -> List[str]:
    """Return the distinct difficulty levels in a task set, sorted."""
    return sorted({t.difficulty for t in load_task_set(version)})