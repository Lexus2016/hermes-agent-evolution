"""Tests for cost-aware routing integration in the orchestrator (#798 inc 3).

Verifies that build_worker_task / build_worker_tasks attach model_hint when
complexity is provided, and that the CLI `build --complexity` flag works.
"""

import json
import os
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


@pytest.fixture(autouse=True)
def _isolate_hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))


def test_build_worker_task_without_complexity_has_no_model_hint():
    """When complexity is None, the task dict has no model_hint (back-compat)."""
    from scripts.evolution_orchestrator import build_worker_task

    task = build_worker_task("research sub-task", "angle A")
    assert "model_hint" not in task
    assert task["role"] == "leaf"
    assert "SUB-TASK: research sub-task" in task["goal"]
    assert "angle A" in task["goal"]


def test_build_worker_task_with_complexity_attaches_model_hint():
    """When complexity is provided, route_cost_tier is called and model_hint attached."""
    from scripts.evolution_orchestrator import build_worker_task

    task = build_worker_task("research sub-task", "angle A", complexity="simple lookup")
    assert "model_hint" in task
    hint = task["model_hint"]
    assert "complexity" in hint
    assert "tier" in hint
    assert "model" in hint
    # "lookup" maps to the "simple" complexity bucket
    assert hint["complexity"] == "simple"


def test_build_worker_task_with_complex_complexity():
    """Complex tasks get a higher tier model hint."""
    from scripts.evolution_orchestrator import build_worker_task

    task = build_worker_task(
        "hard problem", "angle A", complexity="complex migration protocol"
    )
    hint = task["model_hint"]
    # "migration" and "protocol" both map to "complex"
    assert hint["complexity"] == "complex"


def test_build_worker_task_with_unknown_complexity_falls_back():
    """Unknown complexity falls back to the standard tier."""
    from scripts.evolution_orchestrator import build_worker_task

    task = build_worker_task("task", "angle", complexity="zzz unknown zzz")
    hint = task["model_hint"]
    assert hint["complexity"] == "unknown"
    assert "tier" in hint


def test_build_worker_tasks_passes_complexity_to_each_task():
    """build_worker_tasks passes complexity through to every worker task."""
    from scripts.evolution_orchestrator import build_worker_tasks

    tasks, dropped = build_worker_tasks(
        "sub-task", ["A", "B", "C"], complexity="simple doc lookup"
    )
    assert dropped == 0
    assert len(tasks) == 3
    for t in tasks:
        assert "model_hint" in t
        assert t["model_hint"]["complexity"] == "simple"


def test_build_worker_tasks_without_complexity_has_no_model_hint():
    """build_worker_tasks without complexity produces tasks without model_hint."""
    from scripts.evolution_orchestrator import build_worker_tasks

    tasks, _ = build_worker_tasks("sub-task", ["A", "B"])
    for t in tasks:
        assert "model_hint" not in t


def test_cmd_build_with_complexity_flag():
    """The CLI `build --complexity` flag adds model_hint to output tasks."""
    from scripts.evolution_orchestrator import main

    # Capture stdout
    import io
    captured = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = captured
    try:
        rc = main([
            "evolution_orchestrator.py",
            "build",
            "--subtask", "test subtask",
            "--angle", "angle A",
            "--complexity", "simple lookup",
        ])
    finally:
        sys.stdout = old_stdout

    assert rc == 0
    data = json.loads(captured.getvalue())
    assert "tasks" in data
    assert len(data["tasks"]) == 1
    assert "model_hint" in data["tasks"][0]
    assert data["tasks"][0]["model_hint"]["complexity"] == "simple"


def test_cmd_build_without_complexity_flag():
    """The CLI `build` without --complexity produces tasks without model_hint."""
    from scripts.evolution_orchestrator import main

    import io
    captured = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = captured
    try:
        rc = main([
            "evolution_orchestrator.py",
            "build",
            "--subtask", "test subtask",
            "--angle", "angle A",
        ])
    finally:
        sys.stdout = old_stdout

    assert rc == 0
    data = json.loads(captured.getvalue())
    assert len(data["tasks"]) == 1
    assert "model_hint" not in data["tasks"][0]


def test_cmd_build_complexity_needs_value():
    """The --complexity flag with no value returns error code 2."""
    from scripts.evolution_orchestrator import main

    rc = main([
        "evolution_orchestrator.py",
        "build",
        "--subtask", "test",
        "--angle", "A",
        "--complexity",
    ])
    assert rc == 2


def test_route_cost_tier_still_works_standalone():
    """The route CLI command still works independently."""
    from scripts.evolution_orchestrator import main

    import io
    captured = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = captured
    try:
        rc = main(["evolution_orchestrator.py", "route", "--complexity", "simple doc"])
    finally:
        sys.stdout = old_stdout

    assert rc == 0
    data = json.loads(captured.getvalue())
    assert data["complexity"] == "simple"
    assert "tier" in data
    assert "model" in data


if __name__ == "__main__":
    pytest.main([__file__, "-v"])