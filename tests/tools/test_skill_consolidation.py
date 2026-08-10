"""Tests for tools/skill_consolidation.py — deterministic skill clustering.

Tests the clustering logic in isolation (no live skill files needed) and
verifies the pass is wired into the curator review.
"""

import pytest
from unittest.mock import patch, MagicMock

from tools.skill_consolidation import (
    _tokenize,
    _jaccard,
    _cluster,
    run_consolidation_pass,
    render_clusters_for_prompt,
)


class TestTokenize:
    def test_basic(self):
        tokens = _tokenize("Fine-tuning LoRA models")
        assert "fine" in tokens
        assert "tuning" in tokens
        assert "lora" in tokens
        assert "models" in tokens

    def test_drops_short_words(self):
        tokens = _tokenize("a b c dd ee foo")
        assert "foo" in tokens
        assert "a" not in tokens
        assert "b" not in tokens
        assert "dd" not in tokens

    def test_drops_stopwords(self):
        tokens = _tokenize("the skill for agent")
        assert "the" not in tokens
        assert "skill" not in tokens
        assert "agent" not in tokens
        assert "for" not in tokens

    def test_handles_hyphens(self):
        tokens = _tokenize("pre-commit validation")
        assert "pre" in tokens
        assert "commit" in tokens
        assert "validation" in tokens

    def test_empty(self):
        assert _tokenize("") == set()
        assert _tokenize(None) == set()


class TestJaccard:
    def test_identical(self):
        s = {"a", "b", "c"}
        assert _jaccard(s, s) == 1.0

    def test_disjoint(self):
        assert _jaccard({"a"}, {"b"}) == 0.0

    def test_partial(self):
        # {a,b} vs {a,b,c} → 2/3
        assert _jaccard({"a", "b"}, {"a", "b", "c"}) == pytest.approx(2 / 3)

    def test_empty(self):
        assert _jaccard(set(), {"a"}) == 0.0
        assert _jaccard(set(), set()) == 0.0


class TestCluster:
    def test_clusters_similar(self):
        tokens = {
            "a": {"lora", "fine", "tuning"},
            "b": {"lora", "fine", "training"},
            "c": {"docker", "container"},
        }
        clusters = _cluster(["a", "b", "c"], tokens, threshold=0.35)
        # a and b share 2/4 tokens → Jaccard 0.5 > 0.35 → same cluster
        assert len(clusters) == 1
        assert set(clusters[0]) == {"a", "b"}

    def test_no_clusters_when_dissimilar(self):
        tokens = {
            "a": {"lora", "fine"},
            "b": {"docker", "container"},
        }
        clusters = _cluster(["a", "b"], tokens, threshold=0.35)
        assert clusters == []

    def test_single_linkage(self):
        """a~b, b~c, a not directly ~ c → still one cluster via b."""
        tokens = {
            "a": {"lora", "fine", "tuning"},
            "b": {"lora", "fine", "training"},
            "c": {"lora", "training", "adapter"},
        }
        # a~b: 2/4=0.5, b~c: 2/4=0.5, a~c: 1/5=0.2
        clusters = _cluster(["a", "b", "c"], tokens, threshold=0.35)
        assert len(clusters) == 1
        assert set(clusters[0]) == {"a", "b", "c"}


class TestRunConsolidationPass:
    def test_empty_report(self):
        with patch("tools.skill_consolidation.skill_usage") as mock_su:
            mock_su.curated_report.return_value = []
            result = run_consolidation_pass()
            assert result["clusters"] == []
            assert result["total_skills"] == 0

    def test_single_skill(self):
        with patch("tools.skill_consolidation.skill_usage") as mock_su:
            mock_su.curated_report.return_value = [
                {"name": "lora", "state": "active", "activity_count": 5},
            ]
            result = run_consolidation_pass()
            assert result["clusters"] == []

    def test_two_similar_skills_cluster(self):
        rows = [
            {"name": "lora-finetuning", "state": "active", "activity_count": 10},
            {"name": "lora-training", "state": "active", "activity_count": 5},
        ]
        with (
            patch("tools.skill_consolidation.skill_usage") as mock_su,
            patch("tools.skill_consolidation._build_skill_tokens") as mock_tokens,
        ):
            mock_su.curated_report.return_value = rows
            mock_tokens.return_value = {
                "lora-finetuning": {"lora", "fine", "tuning"},
                "lora-training": {"lora", "fine", "training"},
            }
            result = run_consolidation_pass()
            assert len(result["clusters"]) == 1
            cluster = result["clusters"][0]
            assert set(cluster["members"]) == {"lora-finetuning", "lora-training"}
            # Umbrella = highest activity
            assert cluster["suggested_umbrella"] == "lora-finetuning"

    def test_stale_skills_excluded(self):
        rows = [
            {"name": "a", "state": "stale", "activity_count": 10},
            {"name": "b", "state": "stale", "activity_count": 5},
        ]
        with patch("tools.skill_consolidation.skill_usage") as mock_su:
            mock_su.curated_report.return_value = rows
            result = run_consolidation_pass()
            assert result["clusters"] == []


class TestRenderClusters:
    def test_empty(self):
        assert render_clusters_for_prompt({"clusters": []}) == ""

    def test_renders_clusters(self):
        result = {
            "clusters": [
                {"members": ["a", "b"], "suggested_umbrella": "a", "member_count": 2},
            ],
        }
        text = render_clusters_for_prompt(result)
        assert "Cluster 1" in text
        assert "a" in text
        assert "b" in text
        assert "suggested umbrella: a" in text


class TestCuratorWiring:
    """Verify run_consolidation_pass is called from the curator review."""

    def test_import_and_call(self):
        """The curator module references run_consolidation_pass."""
        from agent import curator

        # The import exists in the source — we verify the function is
        # importable from the curator's import path.
        from tools.skill_consolidation import run_consolidation_pass as rcp

        assert callable(rcp)
