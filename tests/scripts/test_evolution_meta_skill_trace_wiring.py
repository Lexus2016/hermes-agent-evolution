"""Tests for the #1876 wiring — meta-skill trace recording in audit_latest."""

import json
from pathlib import Path
from unittest.mock import patch

from scripts.evolution_analysis_audit import audit_latest, _record_meta_skill_trace


def test_record_meta_skill_trace_writes_jsonl(tmp_path: Path):
    """_record_meta_skill_trace appends to meta-skill-traces.jsonl."""
    report = {
        "date": "2026-08-09",
        "selected_for_implementation": [
            {"issue_number": 1876},
            {"issue_number": 1874},
        ],
        "effort_budget": 3.0,
    }
    _record_meta_skill_trace(report, tmp_path)
    trace_file = tmp_path / "meta-skill-traces.jsonl"
    assert trace_file.exists()
    data = json.loads(trace_file.read_text().strip())
    assert data["date"] == "2026-08-09"
    assert data["selected"] == 2
    assert data["selected_issue_ids"] == [1876, 1874]
    assert data["merged"] == 0  # not known at audit time


def test_record_meta_skill_trace_missing_selected(tmp_path: Path):
    """Handles a report with no selected_for_implementation gracefully."""
    report = {"date": "2026-08-09"}
    _record_meta_skill_trace(report, tmp_path)
    trace_file = tmp_path / "meta-skill-traces.jsonl"
    assert trace_file.exists()
    data = json.loads(trace_file.read_text().strip())
    assert data["selected"] == 0
    assert data["selected_issue_ids"] == []


def test_audit_latest_records_trace(tmp_path: Path):
    """audit_latest writes a trace when it finds a valid analysis JSON."""
    analysis_dir = tmp_path / "analysis"
    analysis_dir.mkdir()
    report = {
        "date": "2026-08-09",
        "selected_for_implementation": [
            {"issue_number": 123},
        ],
        "effort_budget": 3.0,
    }
    (analysis_dir / "2026-08-09.json").write_text(json.dumps(report), encoding="utf-8")
    violations = audit_latest(tmp_path)
    assert violations == []  # clean report
    trace_file = tmp_path / "meta-skill-traces.jsonl"
    assert trace_file.exists()
    data = json.loads(trace_file.read_text().strip())
    assert data["date"] == "2026-08-09"
    assert data["selected"] == 1
    assert data["selected_issue_ids"] == [123]


def test_record_meta_skill_trace_io_error_swallowed(tmp_path: Path):
    """IO errors in trace writing don't crash the audit."""
    report = {"date": "2026-08-09", "selected_for_implementation": []}
    with patch(
        "scripts.evolution_meta_skill_trace.append_trace",
        side_effect=OSError("disk full"),
    ):
        # Should not raise — OSError is caught
        _record_meta_skill_trace(report, tmp_path)
