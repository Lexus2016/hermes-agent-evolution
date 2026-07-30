#!/usr/bin/env python3
"""Fixed, versioned evaluation task set for the evolution eval harness (#1514).

Step 1 of the eval harness (decomposed from #1481). This module defines the
**data layer** — a fixed set of representative tasks drawn from real evolution
cycle work — that the harness runner (Step 2 / #1515) and null-agent baseline
(Step 3 / #1516) will consume.

Design: pure data + loader, no runner, no I/O, no agent code. Tasks are
versioned so eval runs are reproducible.

Usage::

    from scripts.evolution_eval_tasks import load_task_set
    tasks = load_task_set("1.0")
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from typing import List

TASK_SET_VERSION: str = "1.0"


@dataclass(frozen=True)
class EvalTask:
    """A single evaluation task in the fixed task set.

    Fields: id (unique), prompt (instruction), expected_tools (tool calls for
    trajectory grading), category (code/search/file-ops/etc.), difficulty
    (easy/medium/hard), expected_result_pattern (optional regex).
    """

    id: str
    prompt: str
    expected_tools: List[str] = field(default_factory=list)
    category: str = ""
    difficulty: str = "easy"
    expected_result_pattern: str = ""


# ---------------------------------------------------------------------------
# Versioned task definitions — never mutate a published version.
# ---------------------------------------------------------------------------

_TASK_SETS: dict[str, List[EvalTask]] = {}


def _v1() -> list[EvalTask]:
    """8 tasks, 4 categories (search, file-ops, code, reasoning)."""
    return [
        EvalTask(
            "eval-001",
            "Search the codebase for all Python files containing the function name 'merge_gate' and report the file paths and line numbers.",
            ["search_files"],
            "search",
            "easy",
            r"merge_gate",
        ),
        EvalTask(
            "eval-002",
            "Create a new file 'hello.py' with a main() function that prints 'Hello, World!' and has an if __name__ == '__main__' guard.",
            ["write_file"],
            "file-ops",
            "easy",
            r"__main__",
        ),
        EvalTask(
            "eval-003",
            "Read the file 'scripts/evolution_merge_gate.py' and summarize what the DEFAULT_MAX_LINES constant controls and its default value.",
            ["read_file"],
            "file-ops",
            "easy",
            r"200",
        ),
        EvalTask(
            "eval-004",
            "Find all TODO comments in the tests/ directory and list each one with its file path and the surrounding comment text.",
            ["search_files"],
            "search",
            "medium",
            r"TODO",
        ),
        EvalTask(
            "eval-005",
            "Run the test suite for the evolution scripts and report which tests pass and which fail.",
            ["terminal"],
            "code",
            "medium",
            r"pass(ed)?|fail(ed)?",
        ),
        EvalTask(
            "eval-006",
            "Add a new function called 'count_lines' to a file that takes a file path and returns the number of lines. Include a docstring and type hints.",
            ["read_file", "patch"],
            "code",
            "medium",
            r"def\s+count_lines",
        ),
        EvalTask(
            "eval-007",
            "Analyze the git log for the last 5 commits and summarize what changes were made, including commit messages and affected files.",
            ["terminal"],
            "reasoning",
            "hard",
            r"commit",
        ),
        EvalTask(
            "eval-008",
            "Given a list of issue titles, classify each as 'bug', 'feature', 'enhancement', or 'documentation' based on the title prefix.",
            [],
            "reasoning",
            "hard",
            r"(bug|feature|enhancement|documentation)",
        ),
    ]


_TASK_SETS["1.0"] = _v1()


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def load_task_set(version: str = TASK_SET_VERSION) -> List[EvalTask]:
    """Return the task list for *version* (shallow copy; raises KeyError if unknown)."""
    if version not in _TASK_SETS:
        raise KeyError(
            f"Unknown task-set version {version!r}. Available: {', '.join(sorted(_TASK_SETS))}"
        )
    return list(_TASK_SETS[version])


def available_versions() -> List[str]:
    """Return all registered task-set versions, sorted."""
    return sorted(_TASK_SETS)


def task_to_dict(task: EvalTask) -> dict:
    """Convert an :class:`EvalTask` to a plain dict (for JSON serialization)."""
    return asdict(task)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _main() -> int:
    parser = argparse.ArgumentParser(
        description="List tasks in the evolution eval task set."
    )
    parser.add_argument(
        "--version",
        default=TASK_SET_VERSION,
        help=f"Task-set version (default: {TASK_SET_VERSION})",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output as JSON array instead of text table.",
    )
    args = parser.parse_args()

    try:
        tasks = load_task_set(args.version)
    except KeyError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        json.dump([task_to_dict(t) for t in tasks], sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        print(f"Task set v{args.version} — {len(tasks)} tasks\n")
        for t in tasks:
            tools = ", ".join(t.expected_tools) if t.expected_tools else "(none)"
            preview = t.prompt[:50].replace("\n", " ") + (
                "..." if len(t.prompt) > 50 else ""
            )
            print(
                f"{t.id:<10} {t.category:<12} {t.difficulty:<10} {tools:<30} {preview}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
