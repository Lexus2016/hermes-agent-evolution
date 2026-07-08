"""Tests for scripts/evolution_draft_selector.py — parallel draft + cost routing (#798)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from evolution_draft_selector import (  # noqa: E402
    COST_TIERS,
    build_draft_tasks,
    route_cost_tier,
    select_best_draft,
)


class TestRouteCostTier:
    def test_low_complexity_economy(self):
        assert route_cost_tier(0.1) == "economy"

    def test_mid_complexity_standard(self):
        assert route_cost_tier(0.5) == "standard"

    def test_high_complexity_frontier(self):
        assert route_cost_tier(0.9) == "frontier"

    def test_boundary_and_clamp(self):
        assert route_cost_tier(0.0) == "economy"
        assert route_cost_tier(0.3) == "standard"
        assert route_cost_tier(0.7) == "frontier"
        assert route_cost_tier(1.0) == "frontier"
        assert route_cost_tier(-0.5) == "economy"
        assert route_cost_tier(1.5) == "frontier"

    def test_all_tiers_used(self):
        results = {route_cost_tier(c) for c in (0.0, 0.5, 0.9)}
        assert results == set(COST_TIERS)


class TestBuildDraftTasks:
    def test_creates_n_identical_tasks(self):
        tasks = build_draft_tasks("Write a function", 3)
        assert len(tasks) == 3
        assert all(t["goal"] == "Write a function" for t in tasks)

    def test_default_n_is_2(self):
        assert len(build_draft_tasks("goal")) == 2

    def test_n_below_1_clamped_to_1(self):
        assert len(build_draft_tasks("goal", 0)) == 1

    def test_role_and_toolsets(self):
        assert all(t["role"] == "leaf" for t in build_draft_tasks("g", 2))
        assert build_draft_tasks("g", 1)[0]["toolsets"] == ["file"]
        assert build_draft_tasks("g", 1, toolsets=["web"])[0]["toolsets"] == ["web"]

    def test_context(self):
        assert (
            build_draft_tasks("g", 1, context="custom ctx")[0]["context"]
            == "custom ctx"
        )
        t = build_draft_tasks("A very long goal " * 20, 1)[0]
        assert t["context"] == ("A very long goal " * 20)[:200]

    def test_tasks_are_copies(self):
        tasks = build_draft_tasks("g", 2)
        tasks[0]["goal"] = "changed"
        assert tasks[1]["goal"] == "g"


class TestSelectBestDraft:
    def test_empty_returns_nothing(self):
        r = select_best_draft([])
        assert r["selected_index"] == -1 and r["result"] is None

    def test_with_scores_picks_highest(self):
        results = [{"summary": "a"}, {"summary": "b"}, {"summary": "c"}]
        r = select_best_draft(results, scores=[0.3, 0.9, 0.5])
        assert r["selected_index"] == 1 and r["reason"] == "highest_score"

    def test_heuristic_prefers_completed(self):
        results = [
            {"status": "running", "summary": "x" * 100},
            {"status": "completed", "summary": "short"},
        ]
        r = select_best_draft(results)
        assert r["selected_index"] == 1

    def test_heuristic_longest_summary(self):
        results = [
            {"status": "completed", "summary": "short"},
            {"status": "completed", "summary": "much longer summary"},
        ]
        r = select_best_draft(results)
        assert r["selected_index"] == 1

    def test_score_length_mismatch_falls_back(self):
        results = [{"summary": "a"}, {"summary": "bb"}]
        r = select_best_draft(results, scores=[0.5])
        assert r["reason"] == "heuristic"

    def test_single_result(self):
        r = select_best_draft([{"summary": "only"}])
        assert r["selected_index"] == 0
