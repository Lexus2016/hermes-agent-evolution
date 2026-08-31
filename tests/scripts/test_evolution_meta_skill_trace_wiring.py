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


def test_record_meta_skill_trace_nested_effort_budget_dict_limit(tmp_path: Path):
    """#168: nested effort_budget {\"limit\": ...} must not crash the audit."""
    report = {
        "date": "2026-08-29",
        "effort_budget": {"limit": 3.0, "selected_total": 3.0, "note": "x"},
    }
    _record_meta_skill_trace(report, tmp_path)  # must not raise TypeError
    data = json.loads((tmp_path / "meta-skill-traces.jsonl").read_text().strip())
    assert data["effort_budget"] == 3.0


def test_record_meta_skill_trace_nested_effort_budget_dict_max(tmp_path: Path):
    """#168: nested effort_budget {\"max\": ...} must not crash the audit."""
    report = {
        "date": "2026-08-28",
        "effort_budget": {"max": 3.0, "used": 2.7, "selected_count": 8},
    }
    _record_meta_skill_trace(report, tmp_path)  # must not raise TypeError
    data = json.loads((tmp_path / "meta-skill-traces.jsonl").read_text().strip())
    assert data["effort_budget"] == 3.0


def test_check_effort_budget_shape_tolerates_scalar_and_known_dicts():
    """#168: scalar, limit-dict and max-dict shapes are all readable."""
    from scripts.evolution_analysis_audit import check_effort_budget_shape

    assert check_effort_budget_shape({"effort_budget": 3.0}) == []
    assert check_effort_budget_shape({"effort_budget": {"limit": 3.0}}) == []
    assert check_effort_budget_shape({"effort_budget": {"max": 3.0, "used": 2.7}}) == []
    assert check_effort_budget_shape({}) == []
    assert check_effort_budget_shape({"effort_budget": None}) == []


def test_check_effort_budget_shape_flags_unreadable_shapes():
    """#168: an unreadable shape is a loud violation, not a crash."""
    from scripts.evolution_analysis_audit import (
        audit_analysis,
        check_effort_budget_shape,
    )

    weird_dict = {"effort_budget": {"spent": 2.7}}  # no scalar under known keys
    violations = check_effort_budget_shape(weird_dict)
    assert violations and violations[0].startswith("EFFORT_BUDGET_SHAPE")

    # The audit surfaces it as a violation string instead of dying.
    assert audit_analysis(weird_dict) == violations

    # Non-dict non-scalar (e.g. a string) is flagged too.
    assert check_effort_budget_shape({"effort_budget": "high"})
    # bool is not a valid budget scalar.
    assert check_effort_budget_shape({"effort_budget": True})


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
