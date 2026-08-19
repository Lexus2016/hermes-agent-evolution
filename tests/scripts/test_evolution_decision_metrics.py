# -*- coding: utf-8 -*-
"""Tests for decision-level regression metrics (#2917, slice 1 of #2899)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from evolution_decision_metrics import (  # noqa: E402
    compute_run_metrics,
    scan_trajectories,
)


class TestComputeRunMetrics:
    def test_rates_from_known_calls(self):
        m = compute_run_metrics([
            {"decision": "tool_call", "tool_call_count": 1},
            {"decision": "tool_call", "tool_call_count": 2},
            {"decision": "refusal"},
            {"decision": "content"},
        ])
        assert m["decisions"] == 4
        assert m["tool_calls"] == 3
        assert m["refusals"] == 1
        assert m["tool_call_rate"] == 0.75
        assert m["safety_refusal_rate"] == 0.25

    def test_invariants_hold(self):
        m = compute_run_metrics([
            {"decision": "tool_call", "tool_call_count": 2},
            {"decision": "refusal"},
            {"decision": "content"},
        ])
        assert 0 <= m["tool_call_rate"] <= 1
        assert 0 <= m["safety_refusal_rate"] <= 1
        assert m["measured"] == m["decisions"] == 3

    def test_missing_or_unmeasurable_returns_empty(self):
        assert compute_run_metrics(None) == {}
        assert compute_run_metrics([]) == {}
        assert compute_run_metrics([{"decision": "unknown"}]) == {}


class TestScanTrajectories:
    def test_skips_runs_without_model_calls(self, tmp_path):
        p = tmp_path / "traj.jsonl"
        p.write_text(
            json.dumps({"session_id": "s1", "entries": []}) + "\n"
            + json.dumps({"session_id": "s2", "model_calls": [
                {"decision": "tool_call", "tool_call_count": 1},
                {"decision": "content"},
            ]}) + "\n",
            encoding="utf-8",
        )
        results = scan_trajectories(tmp_path)
        assert len(results) == 1
        assert results[0]["tool_call_rate"] == 0.5

    def test_missing_dir_is_empty(self, tmp_path):
        assert scan_trajectories(tmp_path / "nope") == []
