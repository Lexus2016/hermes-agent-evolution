"""Tests for state-grounded skill retrieval (first increment of #247).

Covers the pure ``rank_skills`` API (deterministic lexical scoring, field
weighting, usage tie-break, filtering, limit) and the ``skills_search`` tool
wrapper end-to-end against an on-disk skills tree.
"""

import json
from unittest.mock import patch

from tools.skill_retrieval import (
    RankedSkill,
    _extract_tags,
    _tokenize,
    rank_skills,
)
from tools.skills_tool import skills_search


# Candidate fixtures — explicit dicts so ranking tests don't touch disk.
CANDIDATES = [
    {
        "name": "docker-deploy",
        "description": "Build and deploy container images to a registry.",
        "category": "ops",
        "tags": ["docker", "deployment", "containers"],
    },
    {
        "name": "git-commit-helper",
        "description": "Write conventional commit messages and stage changes.",
        "category": "git",
        "tags": ["git", "commit"],
    },
    {
        "name": "pdf-extract",
        "description": "Extract text and tables from PDF documents.",
        "category": "docs",
        "tags": ["pdf", "extraction"],
    },
]


class TestTokenize:
    def test_lowercases_and_splits(self):
        assert _tokenize("Deploy Docker Image") == ["deploy", "docker", "image"]

    def test_drops_stop_words_and_tiny_tokens(self):
        # "the", "to", "a" are stop/short; "ci" (2 chars) is kept.
        assert _tokenize("the ci to a registry") == ["ci", "registry"]

    def test_empty(self):
        assert _tokenize("") == []
        assert _tokenize("   ") == []


class TestRankSkills:
    def test_matches_relevant_skill_first(self):
        out = rank_skills(
            "I need to deploy a docker container",
            candidates=CANDIDATES,
            use_usage_signal=False,
        )
        assert out, "expected at least one match"
        assert out[0].name == "docker-deploy"

    def test_zero_score_candidates_dropped(self):
        # Query is only relevant to the pdf skill.
        out = rank_skills(
            "extract tables from a pdf",
            candidates=CANDIDATES,
            use_usage_signal=False,
        )
        names = [r.name for r in out]
        assert names == ["pdf-extract"]

    def test_empty_query_returns_empty(self):
        assert rank_skills("", candidates=CANDIDATES) == []
        # A query made only of stop words has no scorable terms.
        assert rank_skills("the and for with", candidates=CANDIDATES) == []

    def test_name_weighted_above_description(self):
        # "alpha" only in the name of skill A; "alpha" only in the description
        # of skill B. The name hit must outrank the description hit.
        cands = [
            {"name": "alpha", "description": "unrelated text", "category": "x"},
            {"name": "beta", "description": "this mentions alpha once", "category": "x"},
        ]
        out = rank_skills("alpha", candidates=cands, use_usage_signal=False)
        assert [r.name for r in out] == ["alpha", "beta"]
        assert out[0].score > out[1].score

    def test_tags_contribute_to_score(self):
        cands = [
            {"name": "s1", "description": "nothing", "category": "x", "tags": ["kubernetes"]},
            {"name": "s2", "description": "nothing", "category": "x", "tags": ["unrelated"]},
        ]
        out = rank_skills("kubernetes", candidates=cands, use_usage_signal=False)
        assert [r.name for r in out] == ["s1"]

    def test_limit_caps_results(self):
        cands = [
            {"name": f"deploy-{i}", "description": "deploy things", "category": "ops"}
            for i in range(10)
        ]
        out = rank_skills("deploy", candidates=cands, limit=3, use_usage_signal=False)
        assert len(out) == 3

    def test_matched_terms_reported(self):
        out = rank_skills(
            "deploy docker", candidates=CANDIDATES, use_usage_signal=False
        )
        top = out[0]
        assert top.name == "docker-deploy"
        assert set(top.matched_terms) >= {"deploy", "docker"}

    def test_stable_tie_break_by_name(self):
        # Identical relevance -> alphabetical by name.
        cands = [
            {"name": "zeta", "description": "deploy", "category": "x"},
            {"name": "alpha", "description": "deploy", "category": "x"},
        ]
        out = rank_skills("deploy", candidates=cands, use_usage_signal=False)
        assert [r.name for r in out] == ["alpha", "zeta"]

    def test_usage_boost_breaks_lexical_tie(self):
        cands = [
            {"name": "zeta", "description": "deploy", "category": "x"},
            {"name": "alpha", "description": "deploy", "category": "x"},
        ]
        # Without usage, alpha wins on name tie-break. With usage favoring zeta,
        # zeta should overtake alpha.
        fake_usage = {
            "zeta": {"use_count": 9, "view_count": 0, "patch_count": 0},
            "alpha": {"use_count": 0, "view_count": 0, "patch_count": 0},
        }
        with patch("tools.skill_usage.load_usage", return_value=fake_usage):
            out = rank_skills("deploy", candidates=cands, use_usage_signal=True)
        assert out[0].name == "zeta"

    def test_usage_boost_never_surfaces_irrelevant_skill(self):
        # A wildly-used but irrelevant skill must NOT appear for an unrelated query.
        cands = [
            {"name": "popular-but-irrelevant", "description": "knitting patterns", "category": "craft"},
            {"name": "docker-deploy", "description": "deploy containers", "category": "ops"},
        ]
        fake_usage = {
            "popular-but-irrelevant": {"use_count": 999, "view_count": 999, "patch_count": 0},
        }
        with patch("tools.skill_usage.load_usage", return_value=fake_usage):
            out = rank_skills("deploy a container", candidates=cands, use_usage_signal=True)
        assert [r.name for r in out] == ["docker-deploy"]

    def test_candidate_without_name_skipped(self):
        cands = [
            {"description": "deploy stuff", "category": "ops"},
            {"name": "real", "description": "deploy stuff", "category": "ops"},
        ]
        out = rank_skills("deploy", candidates=cands, use_usage_signal=False)
        assert [r.name for r in out] == ["real"]

    def test_returns_ranked_skill_dataclass(self):
        out = rank_skills("deploy docker", candidates=CANDIDATES, use_usage_signal=False)
        assert all(isinstance(r, RankedSkill) for r in out)


class TestExtractTags:
    def test_metadata_hermes_tags_preferred(self):
        fm = {
            "tags": ["toplevel"],
            "metadata": {"hermes": {"tags": ["nested-a", "nested-b"]}},
        }
        assert _extract_tags(fm) == ["nested-a", "nested-b"]

    def test_toplevel_tags_fallback(self):
        assert _extract_tags({"tags": ["a", "b"]}) == ["a", "b"]

    def test_comma_string_tags(self):
        assert _extract_tags({"tags": "a, b, c"}) == ["a", "b", "c"]

    def test_bracketed_string_tags(self):
        assert _extract_tags({"tags": "[a, b]"}) == ["a", "b"]

    def test_no_tags(self):
        assert _extract_tags({}) == []


def _make_skill(skills_dir, name, description, tags=None, body="Do the thing."):
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    tag_block = ""
    if tags:
        tag_list = ", ".join(tags)
        tag_block = f"metadata:\n  hermes:\n    tags: [{tag_list}]\n"
    content = f"""\
---
name: {name}
description: {description}
{tag_block}---

# {name}

{body}
"""
    (skill_dir / "SKILL.md").write_text(content)
    return skill_dir


class TestSkillsSearchTool:
    def test_ranks_live_skills(self, tmp_path):
        # The registry loader reads skills_tool.SKILLS_DIR directly, so patching
        # it is enough to drive ranking against an on-disk tree end-to-end.
        _make_skill(tmp_path, "docker-deploy", "Deploy containers to a registry.", tags=["docker"])
        _make_skill(tmp_path, "pdf-extract", "Extract text from PDF files.", tags=["pdf"])
        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            result = json.loads(skills_search("deploy a docker container", limit=5))
        assert result["success"] is True
        assert result["count"] >= 1
        assert result["skills"][0]["name"] == "docker-deploy"

    def test_empty_query_errors(self):
        result = json.loads(skills_search("", limit=5))
        assert result.get("success") is False
        assert "query" in result.get("error", "")

    def test_no_match_returns_empty_list(self, tmp_path):
        _make_skill(tmp_path, "pdf-extract", "Extract text from PDF files.", tags=["pdf"])
        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            result = json.loads(skills_search("brew a cup of coffee", limit=5))
        assert result["success"] is True
        assert result["skills"] == []
        assert result["count"] == 0

    def test_invalid_limit_defaults(self, tmp_path):
        _make_skill(tmp_path, "docker-deploy", "Deploy containers.", tags=["docker"])
        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            result = json.loads(skills_search("docker", limit=0))
        assert result["success"] is True
