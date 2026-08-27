# -*- coding: utf-8 -*-
"""Unit tests for evolution.lib.skill_outcome_logger (#3218)."""

import pytest
from evolution.lib.skill_outcome_logger import (
    _SkillOutcomeStore,
    get_skill_stats,
    is_skill_demoted,
    promote_skill,
    record_skill_outcome,
)
from evolution.lib.trigger_matcher import get_matching_skills


class TestSkillOutcomeLogger:
    def test_record_single_outcome(self):
        store = _SkillOutcomeStore()
        rec = store.record_outcome("deploy-app", 0.9, True, revision="v1")
        assert rec["triggered_use_count"] == 1
        assert rec["triggered_success_count"] == 1
        assert rec["success_rate"] == 1.0
        assert not store.is_demoted("deploy-app")

    def test_demotion_after_5_uses_under_half_success(self):
        store = _SkillOutcomeStore()
        # 1 success, 4 failures = 20% success rate over 5 uses
        store.record_outcome("flake-skill", 0.8, True)
        store.record_outcome("flake-skill", 0.8, False)
        store.record_outcome("flake-skill", 0.8, False)
        store.record_outcome("flake-skill", 0.8, False)
        assert not store.is_demoted("flake-skill")  # 4 uses, not yet demoted (needs 5)
        rec = store.record_outcome("flake-skill", 0.8, False)
        assert rec["triggered_use_count"] == 5
        assert rec["success_rate"] == pytest.approx(0.2)
        assert rec["demoted"] is True
        assert store.is_demoted("flake-skill") is True

    def test_re_promotion_clears_demotion(self):
        store = _SkillOutcomeStore()
        for _ in range(5):
            store.record_outcome("flake-skill", 0.8, False)
        assert store.is_demoted("flake-skill") is True

        promoted = store.promote_skill("flake-skill")
        assert promoted["demoted"] is False
        assert store.is_demoted("flake-skill") is False

    def test_persistence_roundtrip(self, tmp_path):
        f = tmp_path / "outcomes.json"
        store1 = _SkillOutcomeStore(f)
        store1.record_outcome("test-skill", 0.95, True, revision="r1")
        assert f.is_file()

        store2 = _SkillOutcomeStore(f)
        stats = store2.get_stats("test-skill")
        assert stats["triggered_use_count"] == 1
        assert stats["triggered_success_count"] == 1
        assert stats["revision"] == "r1"


class TestTriggerMatcherDemotionIntegration:
    def test_demoted_skills_excluded_from_matches(self):
        # Mark flaky-skill as demoted
        for _ in range(5):
            record_skill_outcome("flaky-skill", 0.9, False)
        assert is_skill_demoted("flaky-skill") is True

        skills = [
            {"name": "flaky-skill", "triggers": [{"type": "goal_contains", "values": ["test"], "weight": 1.0}]},
            {"name": "good-skill", "triggers": [{"type": "goal_contains", "values": ["test"], "weight": 1.0}]},
        ]
        matches = get_matching_skills({"goal": "run test workflow"}, skills, threshold=0.7)
        assert len(matches) == 1
        assert matches[0][0]["name"] == "good-skill"
