"""Tests for evolution_heuristic_extract (#1359 — ERL Slice A)."""

import json
import os
import tempfile
from pathlib import Path

import pytest

from scripts.evolution_heuristic_extract import (
    _build_heuristics,
    _extract_patterns,
    _load_trajectories,
    extract_heuristics,
    run,
)


def _write_trajectory(dir_path: str, name: str, entries: list) -> str:
    """Write a trajectory file with the given entries list."""
    fpath = os.path.join(dir_path, name)
    with open(fpath, "w") as f:
        json.dump({"entries": entries}, f)
    return fpath


class TestLoadTrajectories:
    def test_empty_dir_returns_empty_list(self, tmp_path):
        assert _load_trajectories(str(tmp_path)) == []

    def test_loads_dict_with_entries(self, tmp_path):
        _write_trajectory(str(tmp_path), "a.json", [
            {"tool": "terminal", "result_status": "success"},
            {"tool": "read_file", "result_status": "error"},
        ])
        entries = _load_trajectories(str(tmp_path))
        assert len(entries) == 2
        assert entries[0]["tool"] == "terminal"
        assert entries[0]["_source_file"] == "a.json"

    def test_loads_bare_list_format(self, tmp_path):
        fpath = os.path.join(str(tmp_path), "b.json")
        with open(fpath, "w") as f:
            json.dump([{"tool": "patch", "result_status": "success"}], f)
        entries = _load_trajectories(str(tmp_path))
        assert len(entries) == 1
        assert entries[0]["tool"] == "patch"

    def test_skips_invalid_json(self, tmp_path):
        with open(os.path.join(str(tmp_path), "bad.json"), "w") as f:
            f.write("{not json")
        _write_trajectory(str(tmp_path), "good.json", [
            {"tool": "terminal", "result_status": "success"},
        ])
        entries = _load_trajectories(str(tmp_path))
        assert len(entries) == 1


class TestExtractPatterns:
    def test_single_entry(self):
        entries = [{"tool": "terminal", "result_status": "success", "_source_file": "a.json"}]
        groups = _extract_patterns(entries)
        assert "terminal:success" in groups
        assert groups["terminal:success"]["total_count"] == 1
        assert groups["terminal:success"]["success_count"] == 1

    def test_multi_trajectory_shared_pattern(self):
        entries = [
            {"tool": "terminal", "result_status": "success", "_source_file": "a.json"},
            {"tool": "terminal", "result_status": "success", "_source_file": "b.json"},
            {"tool": "terminal", "result_status": "error", "_source_file": "b.json"},
        ]
        groups = _extract_patterns(entries)
        assert groups["terminal:success"]["total_count"] == 2
        assert groups["terminal:success"]["success_count"] == 2
        assert groups["terminal:success"]["source_files"] == {"a.json", "b.json"}
        assert groups["terminal:error"]["total_count"] == 1


class TestBuildHeuristics:
    def test_min_frequency_filters(self):
        groups = {
            "terminal:success": {"source_files": {"a.json"}, "success_count": 1, "total_count": 1},
            "read_file:success": {"source_files": {"a.json", "b.json"}, "success_count": 2, "total_count": 2},
        }
        heuristics = _build_heuristics(groups, min_frequency=2)
        assert len(heuristics) == 1
        assert heuristics[0]["pattern"] == "read_file:success"

    def test_heuristic_fields(self):
        groups = {
            "terminal:success": {
                "source_files": {"a.json", "b.json"},
                "success_count": 2,
                "total_count": 2,
            }
        }
        heuristics = _build_heuristics(groups, min_frequency=1)
        assert len(heuristics) == 1
        h = heuristics[0]
        assert h["pattern"] == "terminal:success"
        assert h["tool"] == "terminal"
        assert h["status"] == "success"
        assert h["frequency"] == 2
        assert h["success_rate"] == 1.0
        assert h["outcome_score"] > 0
        assert "source_trajectories" in h
        assert "recommendation" in h

    def test_ranking_by_outcome_score(self):
        groups = {
            "terminal:success": {
                "source_files": {"a.json"},
                "success_count": 1,
                "total_count": 1,
            },
            "read_file:success": {
                "source_files": {"a.json", "b.json", "c.json"},
                "success_count": 3,
                "total_count": 3,
            },
        }
        heuristics = _build_heuristics(groups, min_frequency=1)
        # read_file:success should rank higher (higher outcome_score due to freq)
        assert heuristics[0]["pattern"] == "read_file:success"


class TestExtractHeuristicsIntegration:
    def test_empty_trajectory_set(self, tmp_path):
        result = extract_heuristics(str(tmp_path))
        assert result == []

    def test_full_pipeline(self, tmp_path):
        _write_trajectory(str(tmp_path), "day1.json", [
            {"tool": "terminal", "result_status": "success"},
            {"tool": "read_file", "result_status": "error"},
        ])
        _write_trajectory(str(tmp_path), "day2.json", [
            {"tool": "terminal", "result_status": "success"},
            {"tool": "patch", "result_status": "success"},
        ])
        heuristics = extract_heuristics(str(tmp_path), min_frequency=2)
        # terminal:success appears 2x → included; others appear 1x → filtered
        assert len(heuristics) == 1
        assert heuristics[0]["pattern"] == "terminal:success"
        assert heuristics[0]["frequency"] == 2


class TestRun:
    def test_writes_output_file(self, tmp_path):
        tdir = str(tmp_path / "traj")
        odir = str(tmp_path / "out")
        os.makedirs(tdir)
        _write_trajectory(tdir, "a.json", [
            {"tool": "terminal", "result_status": "success"},
            {"tool": "terminal", "result_status": "success"},
        ])
        out_path = run(
            trajectories_dir=tdir,
            output_dir=odir,
            min_frequency=1,
        )
        assert os.path.exists(out_path)
        with open(out_path) as f:
            data = json.load(f)
        assert data["heuristic_count"] == 1
        assert len(data["heuristics"]) == 1
        assert data["heuristics"][0]["pattern"] == "terminal:success"
