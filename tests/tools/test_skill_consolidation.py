"""Tests for tools/skill_consolidation.py — deterministic skill clustering."""

from unittest.mock import patch

from tools.skill_consolidation import (
    _tokenize,
    _jaccard,
    _cluster,
    run_consolidation_pass,
    render_clusters_for_prompt,
)


def test_tokenize():
    assert {"fine", "tuning", "lora"} <= _tokenize("Fine-tuning LoRA")
    assert _tokenize("") == set() and _tokenize(None) == set()
    assert not (_tokenize("the skill a foo") & {"the", "skill", "a"})


def test_jaccard():
    assert _jaccard({"a", "b"}, {"a", "b"}) == 1.0
    assert _jaccard({"a"}, {"b"}) == 0.0
    assert _jaccard(set(), {"a"}) == 0.0


def test_cluster():
    tokens = {"a": {"lora", "fine", "tuning"}, "b": {"lora", "fine", "training"}, "c": {"docker"}}
    clusters = _cluster(["a", "b", "c"], tokens, threshold=0.35)
    assert len(clusters) == 1 and set(clusters[0]) == {"a", "b"}
    # single-linkage: a~b, b~c → one cluster
    tokens["c"] = {"lora", "training", "adapter"}
    assert len(_cluster(["a", "b", "c"], tokens, threshold=0.35)) == 1


def test_run_consolidation_pass():
    with patch("tools.skill_consolidation.skill_usage") as mock_su:
        mock_su.curated_report.return_value = []
        assert run_consolidation_pass()["clusters"] == []
    rows = [{"name": "lora-ft", "state": "active", "activity_count": 10},
            {"name": "lora-tr", "state": "active", "activity_count": 5}]
    with patch("tools.skill_consolidation.skill_usage") as mock_su, \
         patch("tools.skill_consolidation._build_skill_tokens") as mock_tok:
        mock_su.curated_report.return_value = rows
        mock_tok.return_value = {"lora-ft": {"lora", "fine"}, "lora-tr": {"lora", "fine"}}
        result = run_consolidation_pass()
        assert len(result["clusters"]) == 1
        assert result["clusters"][0]["suggested_umbrella"] == "lora-ft"


def test_render_clusters():
    assert render_clusters_for_prompt({"clusters": []}) == ""
    text = render_clusters_for_prompt({"clusters": [{"members": ["a", "b"], "suggested_umbrella": "a", "member_count": 2}]})
    assert "Cluster 1" in text and "suggested umbrella: a" in text