"""Tests for skill_view missing/renamed-skill suggestions (#232)."""

from tools.skills_tool import _suggest_skill_names

NAMES = [
    "evolution/evolution-research",
    "evolution/evolution-analysis",
    "web/web-search",
    "git/commit-helper",
]


class TestSuggestSkillNames:
    def test_typo_fuzzy_match(self):
        # close misspelling -> the real skill ranks first
        out = _suggest_skill_names("web/web-serch", NAMES)
        assert "web/web-search" in out

    def test_renamed_leaf_substring_fallback(self):
        # no fuzzy hit on the full path, but the leaf substring catches it
        out = _suggest_skill_names("research", NAMES)
        assert "evolution/evolution-research" in out

    def test_empty_inputs(self):
        assert _suggest_skill_names("", NAMES) == []
        assert _suggest_skill_names("x", []) == []

    def test_no_match_returns_empty(self):
        assert _suggest_skill_names("zzz-nonexistent-qqq", NAMES) == []

    def test_caps_at_n(self):
        many = [f"cat/skill-{i}" for i in range(10)]
        assert len(_suggest_skill_names("skill", many, n=3)) <= 3
