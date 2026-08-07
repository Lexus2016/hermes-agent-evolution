"""E2E test for Slice C (#1775) — Durability wired into the evolution funnel.

Simulates a mid-funnel crash and verifies resume from the last checkpoint.
Uses a temp HERMES_HOME and real filesystem checkpoints (no mocks).
"""

import json
import sys
from pathlib import Path

from agent.durability import FileDurabilityBackend, MemoryDurabilityRegistry

_scripts_dir = Path(__file__).resolve().parent.parent.parent / "scripts"
if str(_scripts_dir) not in sys.path:
    sys.path.insert(0, str(_scripts_dir))

from evolution_funnel import compute_funnel  # noqa: E402


def _ws(evolution_dir: Path, stage: str, date: str, data: object) -> None:
    """Write a stage report file."""
    d = evolution_dir / stage
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{date}.json").write_text(json.dumps(data), encoding="utf-8")


def test_funnel_durability_crash_resume(tmp_path: Path):
    """Mid-funnel crash resumes from the last checkpoint, not from scratch."""
    date = "2026-08-07"
    edir = tmp_path / "evolution"
    edir.mkdir(parents=True)

    _ws(
        edir,
        "issues",
        date,
        {"issues_created": [{"number": 101}, {"number": 102}], "total_proposals": 10},
    )
    _ws(
        edir,
        "analysis",
        date,
        {
            "selected_for_implementation": [
                {"issue_number": 101, "selected_reason": "score"}
            ],
            "rejected": [],
        },
    )
    _ws(edir, "integration", date, {"merged": [{"pr": 999}], "skipped": []})
    _ws(edir, "introspection", date, [{"pattern": "retry_spiral", "count": 2}])

    cp_dir = tmp_path / "checkpoints"
    reg = MemoryDurabilityRegistry()
    reg.register("file", FileDurabilityBackend(base_dir=cp_dir))

    r1 = compute_funnel(edir, date, durability_registry=reg, durability_backend="file")
    assert r1["issues_created"] == 2 and r1["selected"] == 1 and r1["merged"] == 1
    assert r1["introspection_patterns"] == 1
    assert len(list(cp_dir.glob("*.json"))) == 4

    # Simulate crash: delete stage reports (checkpoints survive)
    for stage in ("issues", "analysis", "integration", "introspection"):
        f = edir / stage / f"{date}.json"
        if f.exists():
            f.unlink()

    # Re-run: replays from checkpoints, not from deleted files
    r2 = compute_funnel(edir, date, durability_registry=reg, durability_backend="file")
    assert r2["issues_created"] == r1["issues_created"]
    assert r2["selected"] == r1["selected"]
    assert r2["merged"] == r1["merged"]
    assert r2["introspection_patterns"] == r1["introspection_patterns"]
    assert r2["selected_issue_ids"] == r1["selected_issue_ids"]


def test_funnel_no_durability_is_noop(tmp_path: Path):
    """Without durability opt-in, compute_funnel behaves identically."""
    date = "2026-08-07"
    edir = tmp_path / "evolution"
    edir.mkdir(parents=True)
    _ws(edir, "issues", date, {"issues_created": [{"number": 1}], "total_proposals": 3})
    _ws(
        edir,
        "analysis",
        date,
        {"selected_for_implementation": [{"issue_number": 1}], "rejected": []},
    )
    record = compute_funnel(edir, date)
    assert record["issues_created"] == 1 and record["selected"] == 1
    assert record["merged"] == 0 and record["introspection_patterns"] == 0


def test_funnel_noop_backend_unchanged(tmp_path: Path):
    """Opting in with 'noop' backend is byte-identical to not opting in."""
    date = "2026-08-07"
    edir = tmp_path / "evolution"
    edir.mkdir(parents=True)
    _ws(
        edir,
        "issues",
        date,
        {"issues_created": [{"number": 1}, {"number": 2}], "total_proposals": 5},
    )
    _ws(edir, "analysis", date, {"selected_for_implementation": []})
    reg = MemoryDurabilityRegistry()
    record = compute_funnel(
        edir, date, durability_registry=reg, durability_backend="noop"
    )
    assert record["issues_created"] == 2 and record["selected"] == 0
    assert not list(tmp_path.rglob("durability"))
