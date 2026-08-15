# -*- coding: utf-8 -*-
"""Unit tests for SkillMOO Pareto Optimization and Pruning Discipline (#2290)."""

import pytest

from evolution.lib.skill_moo import (
    ParetoRankingResult,
    SkillMetricProfile,
    SkillMOOOptimizer,
)


class TestSkillMOOOptimizer:
    """Test suite for multi-objective Pareto optimization and pruning."""

    def test_dominance_logic(self):
        s1 = SkillMetricProfile(
            skill_name="s1", pass_rate=0.9, inference_cost_tokens=100
        )
        s2 = SkillMetricProfile(
            skill_name="s2", pass_rate=0.7, inference_cost_tokens=150
        )
        s3 = SkillMetricProfile(
            skill_name="s3", pass_rate=0.95, inference_cost_tokens=200
        )

        # s1 dominates s2 (higher pass rate, lower cost)
        assert s1.dominates(s2)
        assert not s2.dominates(s1)

        # s1 and s3 do not dominate each other (tradeoff: s3 has higher pass rate but higher cost)
        assert not s1.dominates(s3)
        assert not s3.dominates(s1)

    def test_calculate_non_dominated_fronts(self):
        profiles = [
            SkillMetricProfile(
                skill_name="best_tradeoff_1", pass_rate=0.9, inference_cost_tokens=100
            ),
            SkillMetricProfile(
                skill_name="best_tradeoff_2", pass_rate=0.98, inference_cost_tokens=250
            ),
            SkillMetricProfile(
                skill_name="dominated_1", pass_rate=0.7, inference_cost_tokens=200
            ),
            SkillMetricProfile(
                skill_name="dominated_2", pass_rate=0.6, inference_cost_tokens=300
            ),
        ]
        fronts = SkillMOOOptimizer.calculate_non_dominated_fronts(profiles)
        assert len(fronts) >= 2

        front_0_names = {p.skill_name for p in fronts[0]}
        assert "best_tradeoff_1" in front_0_names
        assert "best_tradeoff_2" in front_0_names
        assert "dominated_1" not in front_0_names

    def test_optimize_and_prune(self):
        profiles = [
            SkillMetricProfile(
                skill_name="s_high_pass", pass_rate=0.95, inference_cost_tokens=150
            ),
            SkillMetricProfile(
                skill_name="s_low_cost", pass_rate=0.85, inference_cost_tokens=50
            ),
            SkillMetricProfile(
                skill_name="s_poor_perf", pass_rate=0.30, inference_cost_tokens=100
            ),
            SkillMetricProfile(
                skill_name="s_dominated_waste",
                pass_rate=0.70,
                inference_cost_tokens=400,
            ),
        ]

        result = SkillMOOOptimizer.optimize_and_prune(
            profiles,
            min_pass_rate=0.5,
            max_fronts_to_keep=1,
        )

        assert "s_high_pass" in result.keep_skills
        assert "s_low_cost" in result.keep_skills
        assert "s_poor_perf" in result.prune_skills
        assert "s_dominated_waste" in result.prune_skills
        assert result.total_cost_saved_tokens >= 500
