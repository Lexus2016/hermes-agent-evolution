"""Tests for the scheduled dream contradiction scan runner (issue #48 slice 1).

The runner is the ``no_agent`` cron entry point
(``evolution-dream-contradiction-scan``, 02:45 daily) — these tests exercise
its real ``main()`` against a temp tqmemory layout and a temp report path.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import evolution_dream_contradiction_scan as runner


def _write_note(root: Path, note_id: str, title: str, created_at: str) -> None:
    d = root / "projects" / "proj-a" / "notes"
    d.mkdir(parents=True, exist_ok=True)
    note = {
        "content": "body",
        "created_at": created_at,
        "note_id": note_id,
        "note_kind": "decision",
        "note_status": "active",
        "project_id": "proj-a",
        "project_name": "proj-a",
        "scope": "project",
        "source_refs": [],
        "tags": ["t"],
        "title": title,
        "updated_at": created_at,
    }
    (d / f"{note_id}.json").write_text(
        json.dumps(note, indent=2) + "\n", encoding="utf-8"
    )


def _argv(tmp_path: Path, report: Path, *extra: str) -> list[str]:
    return ["prog", "--root", str(tmp_path), "--report", str(report), *extra]


def test_main_dry_run_writes_report_no_deprecation(tmp_path: Path) -> None:
    _write_note(tmp_path, "n1", "Tool X failure", "2026-08-01T00:00:00Z")
    _write_note(tmp_path, "n2", "Tool X failure", "2026-08-02T00:00:00Z")
    report = tmp_path / "out" / "report.json"
    code = runner.main(_argv(tmp_path, report, "--dry-run"))
    assert code == 0
    data = json.loads(report.read_text(encoding="utf-8"))
    assert data["contradictions_found"] == 1
    assert data["deprecations_applied"] == 1
    assert data["dry_run"] is True
    # Nothing was superseded on disk.
    note = json.loads(
        (tmp_path / "projects" / "proj-a" / "notes" / "n1.json").read_text()
    )
    assert note["note_status"] == "active"


def test_main_applies_deprecations(tmp_path: Path) -> None:
    _write_note(tmp_path, "n1", "Tool X failure", "2026-08-01T00:00:00Z")
    _write_note(tmp_path, "n2", "Tool X failure", "2026-08-02T00:00:00Z")
    report = tmp_path / "out" / "report.json"
    code = runner.main(_argv(tmp_path, report))
    assert code == 0
    data = json.loads(report.read_text(encoding="utf-8"))
    assert data["contradictions_found"] == 1 and data["deprecations_applied"] == 1
    note = json.loads(
        (tmp_path / "projects" / "proj-a" / "notes" / "n1.json").read_text()
    )
    assert note["note_status"] == "superseded"
    assert note["superseded_by"]["note_id"] == "n2"


def test_main_missing_root_is_clean_noop(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    code = runner.main([
        "prog",
        "--root",
        str(tmp_path / "nope"),
        "--report",
        str(report),
    ])
    assert code == 0
    data = json.loads(report.read_text(encoding="utf-8"))
    assert data["notes_scanned"] == 0 and data["contradictions_found"] == 0
