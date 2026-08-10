"""Tests for evolution_sea_eval (issue #2249)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import evolution_sea_eval as sea  # noqa: E402


def _rec(date, **kw):
    base = {
        "date": date,
        "issues_created": 0,
        "selected": 0,
        "merged": 0,
        "research_proposals": 0,
        "introspection_patterns": 0,
    }
    base.update(kw)
    return base


def test_converging_series_is_genuine_evolution():
    # Same task type, cost strictly decreasing over time -> converging.
    records = [
        _rec("2026-08-01", issues_created=5, execution_cost=10.0),
        _rec("2026-08-02", issues_created=5, execution_cost=7.0),
        _rec("2026-08-03", issues_created=5, execution_cost=4.0),
    ]
    result = sea.compute_convergence(records, min_occurrences=3)
    assert result["genuine_evolution"] is True
    assert result["non_converging_task_types"] == []
    assert result["per_task_type"]["issue-creation"]["trend"] == "converging"


def test_flat_series_is_flagged_non_converging():
    # Same task type, cost stays flat -> accumulating, not evolving.
    records = [
        _rec("2026-08-01", issues_created=5, execution_cost=10.0),
        _rec("2026-08-02", issues_created=5, execution_cost=10.0),
        _rec("2026-08-03", issues_created=5, execution_cost=10.0),
    ]
    result = sea.compute_convergence(records, min_occurrences=3)
    assert result["genuine_evolution"] is False
    assert "issue-creation" in result["non_converging_task_types"]


def test_rising_series_is_diverging():
    records = [
        _rec("2026-08-01", issues_created=5, execution_cost=4.0),
        _rec("2026-08-02", issues_created=5, execution_cost=8.0),
        _rec("2026-08-03", issues_created=5, execution_cost=12.0),
    ]
    result = sea.compute_convergence(records, min_occurrences=3)
    assert result["per_task_type"]["issue-creation"]["trend"] == "diverging"
    assert result["genuine_evolution"] is False


def test_rare_task_type_not_judged():
    # Only 2 occurrences -> below min_occurrences, not flagged.
    records = [
        _rec("2026-08-01", research_proposals=2, execution_cost=5.0),
        _rec("2026-08-02", research_proposals=2, execution_cost=5.0),
    ]
    result = sea.compute_convergence(records, min_occurrences=3)
    assert result["per_task_type"]["research"]["trend"] == "n/a"
    assert result["non_converging_task_types"] == []


def test_cost_proxy_falls_back_to_output_volume():
    # No explicit execution_cost -> proxy derived from output volume.
    records = [
        _rec("2026-08-01", issues_created=2, selected=1, merged=1),
        _rec("2026-08-02", issues_created=2, selected=1, merged=1),
    ]
    result = sea.compute_convergence(records, min_occurrences=2)
    entry = result["per_task_type"]["issue-creation"]
    assert entry["cost_series"] == [4.0, 4.0]
    assert entry["trend"] == "flat"


def test_empty_records():
    result = sea.compute_convergence([])
    assert result["per_task_type"] == {}
    assert result["genuine_evolution"] is True
