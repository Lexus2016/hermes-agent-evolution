"""Tests for scripts/evolution_validation_subset.py + pre-PR wiring (#2638)."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from evolution_pre_pr_test_runner import run_gate  # noqa: E402
from evolution_validation_subset import select_subset, track_validation_cost  # noqa: E402

TASKS = [
    {"id": i, "difficulty": b}
    for i, b in enumerate(["easy"] * 4 + ["medium"] * 4 + ["hard"] * 4)
]


def test_subset_respects_budget_and_difficulty_mix():
    result = select_subset(TASKS, budget_ratio=0.5)
    stats = result["stats"]
    assert stats["subset"] == 6
    # Every band keeps a proportional share (ranking fidelity proxy).
    assert {t["difficulty"] for t in result["tasks"]} == {"easy", "medium", "hard"}


def test_subset_is_deterministic():
    a = select_subset(TASKS, budget_ratio=0.5, seed=7)
    b = select_subset(TASKS, budget_ratio=0.5, seed=7)
    assert [t["id"] for t in a["tasks"]] == [t["id"] for t in b["tasks"]]


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


class _FakeRunner:
    """Injectable subprocess runner recording the commands it executes."""

    def __init__(self, returncode: int = 0):
        self.returncode = returncode
        self.calls: list = []

    def __call__(self, cmd, env):
        self.calls.append(cmd)
        return self.returncode, "ok", ""


def test_run_gate_runs_on_subset_and_tracks_cost(tmp_path, monkeypatch):
    """The real pre-PR gate runs the batch on the subset and tracks cost."""
    fake = _FakeRunner()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    report = run_gate(
        ["agent/foo.py", "hermes_cli/bar.py", "tools/baz.py", "cron/qux.py"],
        Path(__file__).resolve().parents[2],
        runner=fake,
        log_path=tmp_path / "gate.log",
        validation_subset_ratio=0.5,
    )
    assert 0 < len(fake.calls) < 4  # ran on the subset, not the full batch
    assert "validation subset" in report.note
    assert report.validation_cost["record_count"] == 1  # cost aggregated
    cost_file = tmp_path / "evolution" / "validation-cost.jsonl"
    line = json.loads(cost_file.read_text(encoding="utf-8").strip())
    assert line["stage"] == "pre_pr_validation"
    assert line["shards_total"] == 4
