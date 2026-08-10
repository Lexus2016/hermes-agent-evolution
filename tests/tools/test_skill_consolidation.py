"""Tests for tools/skill_consolidation.py — knowledge consolidation pass (#2184).

Tests cover:
  - Tokenization + Jaccard similarity
  - entry_weight: utility + age decay
  - cluster_skills: grouping, threshold, min size
  - recommend_consolidation: candidate selection, demotion
  - run_consolidation_pass: end-to-end
"""

from datetime import datetime, timedelta, timezone

import pytest

from tools.skill_consolidation import (
    SkillEntry,
    ClusterResult,
    _tokenize,
    skill_similarity,
    entry_weight,
    cluster_skills,
    recommend_consolidation,
    run_consolidation_pass,
)


class TestTokenize:
    def test_basic(self):
        tokens = _tokenize("Git commit workflow for versioning")
        assert "git" in tokens
        assert "commit" in tokens
        assert "workflow" in tokens
        assert "versioning" in tokens

    def test_stops_words_removed(self):
        tokens = _tokenize("the skill to use when you are done")
        assert "the" not in tokens
        assert "skill" not in tokens  # stop word
        assert "use" not in tokens  # stop word

    def test_empty(self):
        assert _tokenize("") == set()

    def test_case_insensitive(self):
        assert _tokenize("Python") == _tokenize("python")


class TestSimilarity:
    def test_identical_skills(self):
        a = SkillEntry("a", "git commit workflow")
        b = SkillEntry("b", "git commit workflow")
        assert skill_similarity(a, b) == 1.0

    def test_no_overlap(self):
        a = SkillEntry("a", "docker container deployment")
        b = SkillEntry("b", "calculus integration theorem")
        assert skill_similarity(a, b) == 0.0

    def test_partial_overlap(self):
        a = SkillEntry("a", "git commit push workflow")
        b = SkillEntry("b", "git branch merge workflow")
        sim = skill_similarity(a, b)
        assert 0.0 < sim < 1.0


class TestEntryWeight:
    def test_high_utility_high_weight(self):
        now = datetime.now(timezone.utc)
        e = SkillEntry(
            "s",
            "desc",
            invocation_count=100,
            failure_rate=0.0,
            last_used_at=now.isoformat(),
        )
        w = entry_weight(e, now)
        assert w > 0

    def test_unused_skill_low_weight(self):
        now = datetime.now(timezone.utc)
        e = SkillEntry("s", "desc", invocation_count=0, failure_rate=0.0)
        w = entry_weight(e, now)
        # No utility, no last_used → low weight
        assert w < 0.5

    def test_old_skill_decays(self):
        now = datetime.now(timezone.utc)
        old = now - timedelta(days=60)
        e_recent = SkillEntry(
            "a", "d", invocation_count=10, last_used_at=now.isoformat()
        )
        e_old = SkillEntry("b", "d", invocation_count=10, last_used_at=old.isoformat())
        w_recent = entry_weight(e_recent, now)
        w_old = entry_weight(e_old, now)
        assert w_recent > w_old

    def test_high_failure_rate_reduces_utility(self):
        now = datetime.now(timezone.utc)
        e_good = SkillEntry(
            "a",
            "d",
            invocation_count=10,
            failure_rate=0.0,
            last_used_at=now.isoformat(),
        )
        e_bad = SkillEntry(
            "b",
            "d",
            invocation_count=10,
            failure_rate=0.9,
            last_used_at=now.isoformat(),
        )
        assert entry_weight(e_good, now) > entry_weight(e_bad, now)


class TestClusterSkills:
    def test_clusters_similar(self):
        entries = [
            SkillEntry("git-commit", "git commit workflow for versioning"),
            SkillEntry("git-push", "git push workflow for versioning"),
            SkillEntry("math", "calculus integration derivative theorem"),
        ]
        clusters = cluster_skills(entries, threshold=0.3)
        assert len(clusters) == 1
        assert "git-commit" in clusters[0].members
        assert "git-push" in clusters[0].members
        assert "math" not in clusters[0].members

    def test_no_clusters_below_min_size(self):
        entries = [
            SkillEntry("a", "git commit"),
            SkillEntry("b", "calculus math"),
        ]
        clusters = cluster_skills(entries, threshold=0.5)
        assert clusters == []

    def test_empty_input(self):
        assert cluster_skills([]) == []

    def test_single_entry(self):
        assert cluster_skills([SkillEntry("a", "d")]) == []


class TestRecommendConsolidation:
    def test_picks_highest_weight(self):
        now = datetime.now(timezone.utc)
        entries = [
            SkillEntry(
                "low",
                "git workflow",
                invocation_count=1,
                last_used_at=(now - timedelta(days=10)).isoformat(),
            ),
            SkillEntry(
                "high",
                "git workflow",
                invocation_count=50,
                last_used_at=now.isoformat(),
            ),
        ]
        cluster = ClusterResult(members=["low", "high"])
        result = recommend_consolidation(cluster, entries, now)
        assert result.consolidation_candidate == "high"

    def test_demotion_candidates(self):
        now = datetime.now(timezone.utc)
        entries = [
            SkillEntry(
                "top",
                "git workflow",
                invocation_count=100,
                last_used_at=now.isoformat(),
            ),
            SkillEntry(
                "mid", "git workflow", invocation_count=20, last_used_at=now.isoformat()
            ),
            SkillEntry(
                "low",
                "git workflow",
                invocation_count=1,
                last_used_at=(now - timedelta(days=20)).isoformat(),
            ),
        ]
        cluster = ClusterResult(members=["top", "mid", "low"])
        result = recommend_consolidation(cluster, entries, now)
        assert result.consolidation_candidate == "top"
        assert "low" in result.demotion_candidates


class TestRunConsolidationPass:
    def test_end_to_end(self):
        now = datetime.now(timezone.utc)
        entries = [
            SkillEntry(
                "git-a",
                "git commit push workflow versioning",
                invocation_count=30,
                last_used_at=now.isoformat(),
            ),
            SkillEntry(
                "git-b",
                "git merge branch workflow versioning",
                invocation_count=5,
                last_used_at=now.isoformat(),
            ),
            SkillEntry(
                "math",
                "calculus derivative theorem proof",
                invocation_count=10,
                last_used_at=now.isoformat(),
            ),
        ]
        results = run_consolidation_pass(entries, threshold=0.3, now=now)
        assert len(results) == 1
        cluster = results[0]
        assert "git-a" in cluster.members
        assert "git-b" in cluster.members
        assert cluster.consolidation_candidate in ("git-a", "git-b")

    def test_no_clusters_returns_empty(self):
        entries = [
            SkillEntry("a", "completely unique alpha"),
            SkillEntry("b", "totally different beta"),
        ]
        results = run_consolidation_pass(entries, threshold=0.5)
        assert results == []
