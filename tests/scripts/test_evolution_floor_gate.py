"""Tests for the null-agent floor-test gate (#1809)."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from evolution_merge_gate import check_floor_gate, check_merge_policy_with_quality  # noqa: E402

_FILES = [{"path": "README.md", "additions": 3, "deletions": 2}]


def test_blocks_when_metric_at_floor() -> None:
    """Metric == floor means the null agent achieved it → block."""
    v = check_floor_gate(
        floor_scores={"mean_total": 0.0, "mean_tool_score": 0.0},
        pr_metrics={"mean_total": 0.0, "mean_tool_score": 0.5},
    )
    assert any("FLOOR_GATE_BLOCK" in s and "mean_total" in s for s in v)


def test_passes_when_metrics_above_floor() -> None:
    """All metrics strictly above floor → no violations."""
    floor = {"mean_total": 0.15, "mean_tool_score": 0.1, "mean_result_score": 0.05}
    pr = {"mean_total": 0.30, "mean_tool_score": 0.25, "mean_result_score": 0.20}
    assert check_floor_gate(floor_scores=floor, pr_metrics=pr) == []


def test_skipped_when_no_floor_scores() -> None:
    """No floor-scores sidecar → gate skipped (opt-in)."""
    assert check_floor_gate(floor_scores=None, pr_metrics={"mean_total": 0.0}) == []


def test_fails_closed_when_pr_metrics_missing() -> None:
    """Floor scores exist but PR metrics not provided → fail closed."""
    v = check_floor_gate(floor_scores={"mean_total": 0.0}, pr_metrics=None)
    assert len(v) == 1
    assert "FLOOR_GATE_NO_PR_METRICS" in v[0]


def test_wired_into_quality_check_blocks() -> None:
    """Floor gate violation surfaces through check_merge_policy_with_quality."""
    v = check_merge_policy_with_quality(
        _FILES,
        floor_scores={"mean_total": 0.0},
        pr_metrics={"mean_total": 0.0},
    )
    assert any("FLOOR_GATE_BLOCK" in s for s in v)


def test_wired_skipped_without_floor_scores() -> None:
    """No floor_scores → gate skipped, no FLOOR_GATE violations."""
    v = check_merge_policy_with_quality(_FILES, floor_scores=None, pr_metrics=None)
    assert not any("FLOOR_GATE" in s for s in v)