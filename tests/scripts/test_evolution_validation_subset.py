"""Tests for scripts/evolution_validation_subset.py (#2638)."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from evolution_validation_subset import (  # noqa: E402
    estimate_cost,
    main,
    select_subset,
    track_validation_cost,
)

TASKS = [
    {"id": i, "difficulty": b, "steps": 2}
    for i, b in enumerate(["easy"] * 4 + ["medium"] * 4 + ["hard"] * 4)
]


def test_subset_respects_budget_and_difficulty_mix():
    result = select_subset(TASKS, budget_ratio=0.5)
    stats = result["stats"]
    assert stats["subset"] <= stats["total"]
    assert stats["subset"] == 6
    # Every band keeps a proportional share (ranking fidelity proxy).
    assert {t["difficulty"] for t in result["tasks"]} == {"easy", "medium", "hard"}


def test_subset_is_deterministic():
    a = select_subset(TASKS, budget_ratio=0.5, seed=7)
    b = select_subset(TASKS, budget_ratio=0.5, seed=7)
    assert [t["id"] for t in a["tasks"]] == [t["id"] for t in b["tasks"]]


def test_subset_under_ratio_keeps_all_when_small():
    result = select_subset(
        [{"id": 1, "difficulty": "easy", "steps": 1}], budget_ratio=0.5
    )
    assert result["stats"]["subset"] == 1
    assert result["tasks"][0]["id"] == 1


def test_subset_empty():
    result = select_subset([], budget_ratio=0.5)
    assert result["tasks"] == []
    assert result["stats"]["total"] == 0


def test_subset_invalid_ratio_raises():
    import pytest

    with pytest.raises(ValueError):
        select_subset(TASKS, budget_ratio=0.0)


def test_estimate_cost_counts_steps():
    cost = estimate_cost(TASKS, cost_per_step=0.5)
    assert cost["tasks"] == 12
    assert cost["steps"] == 24
    assert cost["estimated_cost"] == 12.0


def test_track_validation_cost_aggregates_cycles():
    records = [
        {"cycle": "2026-08-15", "validation_cost": 10},
        {"cycle": "2026-08-15", "validation_cost": 5},
        {"cycle": "2026-08-16", "validation_cost": 7},
    ]
    report = track_validation_cost(records)
    assert report["cycles"]["2026-08-15"] == 15
    assert report["total_cost"] == 22
    assert report["record_count"] == 3


def test_cli_reduces_task_list(tmp_path, capsys):
    p = tmp_path / "tasks.json"
    p.write_text(json.dumps(TASKS), encoding="utf-8")
    rc = main([
        "evolution_validation_subset.py",
        "--tasks-json",
        str(p),
        "--budget-ratio",
        "0.5",
    ])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["stats"]["subset"] == 6
    assert out["cost"]["tasks"] == 6
