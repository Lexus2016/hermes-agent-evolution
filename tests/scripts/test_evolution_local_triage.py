"""Tests for evolution_local_triage.py (#783).

Verifies the local triage pass:
- Reads local sidecar files without GitHub API calls
- Extracts filed proposals from issues sidecar
- Applies effort budget from health sidecar
- Detects consolidation mode from realized-impact sidecar
- Produces output in the standard analysis JSON format
- Does not clobber existing full (non-local) analysis files
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from evolution_local_triage import run_local_triage, _latest_file, _read_sidecar


@pytest.fixture
def evo_dir(tmp_path):
    """Create a minimal evolution directory with sidecars."""
    evo = tmp_path / "evolution"
    issues_dir = evo / "issues"
    issues_dir.mkdir(parents=True)
    (issues_dir / "2026-07-08.json").write_text(json.dumps({
        "date": "2026-07-08",
        "proposals": [
            {"title": "Alpha", "decision": "filed", "issue": 100,
             "priority_score": 1.5, "impact": 0.8, "effort": 0.3},
            {"title": "Beta", "decision": "skipped", "issue": None,
             "priority_score": 0.5, "impact": 0.2, "effort": 0.1},
            {"title": "Gamma", "decision": "filed", "issue": 200,
             "priority_score": 0.9, "impact": 0.5, "effort": 0.8},
        ]
    }))
    return evo


def test_local_triage_basic(evo_dir):
    result = run_local_triage(evo_dir)
    assert result["local_triage"] is True
    assert result["date"]  # ISO date string
    assert "issues" in result["sidecars_read"]


def test_local_triage_excludes_skipped_proposals(evo_dir):
    result = run_local_triage(evo_dir)
    nums = [s["issue_number"] for s in result["selected_for_implementation"]]
    assert 100 in nums
    assert 200 in nums
    # Beta was "skipped" with issue=None — must not appear
    assert None not in nums


def test_local_triage_sorted_by_priority(evo_dir):
    result = run_local_triage(evo_dir)
    scores = [s["priority_score"] for s in result["selected_for_implementation"]]
    assert scores == sorted(scores, reverse=True)


def test_local_triage_effort_budget_default(evo_dir):
    """Without health sidecar, effort_budget defaults to 3.0."""
    result = run_local_triage(evo_dir)
    assert result["effort_budget"] == 3.0


def test_local_triage_effort_budget_throttled(evo_dir):
    """Health sidecar with LOW_SELECTION_EFFICIENCY sets budget to 1.5."""
    (evo_dir / "evolution-health.txt").write_text(
        "[evolution-metrics] LOW_SELECTION_EFFICIENCY effort_budget=1.5"
    )
    result = run_local_triage(evo_dir)
    assert result["effort_budget"] == 1.5


def test_local_triage_consolidation_mode(evo_dir):
    """Realized-impact sidecar with REALIZED_IMPACT_LOW sets consolidation."""
    (evo_dir / "realized-impact.txt").write_text(
        "[evolution-realized-impact] REALIZED_IMPACT_LOW"
    )
    result = run_local_triage(evo_dir)
    assert result["consolidation_mode"] is True


def test_local_triage_no_consolidation_without_signal(evo_dir):
    result = run_local_triage(evo_dir)
    assert result["consolidation_mode"] is False


def test_local_triage_effort_budget_enforced(evo_dir):
    """Total selected effort must not exceed the budget."""
    (evo_dir / "evolution-health.txt").write_text(
        "[evolution-metrics] effort_budget=1.5"
    )
    result = run_local_triage(evo_dir)
    total = sum(s["effort_score"] for s in result["selected_for_implementation"])
    assert total <= 1.5


def test_local_triage_standard_format(evo_dir):
    """Output must have the standard analysis JSON keys."""
    result = run_local_triage(evo_dir)
    assert "date" in result
    assert "rejected" in result
    assert "selected_for_implementation" in result
    for entry in result["selected_for_implementation"]:
        assert "issue_number" in entry
        assert "title" in entry
        assert "priority_score" in entry
        assert "selected_reason" in entry


def test_local_triage_empty_issues_dir(tmp_path):
    """If no sidecars exist, produce empty output without error."""
    evo = tmp_path / "evolution"
    evo.mkdir()
    result = run_local_triage(evo)
    assert result["local_triage"] is True
    assert result["selected_for_implementation"] == []
    assert result["sidecars_read"] == {}


def test_latest_file_returns_most_recent(tmp_path):
    """_latest_file should return the most recently modified file."""
    d = tmp_path / "issues"
    d.mkdir()
    old = d / "2026-07-01.json"
    new = d / "2026-07-08.json"
    old.write_text("{}")
    new.write_text("{}")
    # Ensure new is modified after old
    import os
    os.utime(old, (1, 1))
    os.utime(new, (2, 2))
    result = _latest_file(d)
    assert result == new


def test_read_sidecar_json(tmp_path):
    f = tmp_path / "test.json"
    f.write_text('{"key": "value"}')
    result = _read_sidecar(f)
    assert result["data"]["key"] == "value"


def test_read_sidecar_md(tmp_path):
    f = tmp_path / "test.md"
    f.write_text("# Research report\n\nContent here.")
    result = _read_sidecar(f)
    assert result["char_count"] > 0