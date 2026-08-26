# -*- coding: utf-8 -*-
"""Tests for skill trigger extraction/matching (first slice of #3210)."""

from evolution.lib.skill_crystallizer import SkillCrystallizer
from evolution.lib.trigger_matcher import (
    best_matches,
    extract_trigger_metadata,
    parse_trigger_frontmatter,
    render_trigger_frontmatter,
    score_trigger,
    validate_trigger_metadata,
)

_TRACE = {
    "status": "success",
    "session_id": "sess_t",
    "goal": "Migrate database schema and verify indexes",
    "tool_calls": [
        {"name": "file_read", "arguments": "{}"},
        {"name": "terminal", "arguments": "{}"},
        {"name": "terminal", "arguments": "{}"},
        {"name": "web_search", "arguments": "{}"},
    ],
}


class TestTriggerMetadata:
    def test_extract_from_trace(self):
        meta = extract_trigger_metadata(_TRACE)
        assert meta["task_kind"] == "migrate-database"
        assert "file_read" in meta["tools"] and "terminal" in meta["tools"]
        assert meta["intent_signals"]

    def test_validate_rejects_bad_shapes(self):
        assert validate_trigger_metadata(None)[0] is False
        assert validate_trigger_metadata({"task_kind": ""})[0] is False
        assert (
            validate_trigger_metadata({"task_kind": "x", "tools": "not-a-list"})[0]
            is False
        )
        ok, _ = validate_trigger_metadata({
            "task_kind": "x",
            "tools": ["a"],
            "intent_signals": [],
        })
        assert ok is True


class TestFrontmatterRoundTrip:
    def test_render_parse_roundtrip(self):
        meta = extract_trigger_metadata(_TRACE)
        block = render_trigger_frontmatter(meta)
        md = f"---\nname: x\ndescription: y\n{block}\n---\n# Body"
        parsed = parse_trigger_frontmatter(md)
        assert parsed == meta

    def test_parse_returns_none_when_absent_or_malformed(self):
        assert parse_trigger_frontmatter("---\nname: x\n---\n") is None
        assert parse_trigger_frontmatter("---\ntriggers:\n  nope\n---\n") is None

    def test_crystallized_skill_carries_valid_triggers(self):
        candidate = SkillCrystallizer.reflect_on_trace(dict(_TRACE))
        assert candidate is not None
        assert "triggers:" in candidate.skill_markdown
        parsed = parse_trigger_frontmatter(candidate.skill_markdown)
        assert parsed and parsed["task_kind"] == "migrate-database"

    def test_validate_candidate_flags_malformed_triggers(self):
        bad = "---\nname: x\ndescription: y\ntriggers:\n  task_kind: \n---\n"
        from evolution.lib.skill_crystallizer import CrystallizedSkillCandidate

        ok, reason = SkillCrystallizer.validate_candidate(
            CrystallizedSkillCandidate(name="x", description="y", skill_markdown=bad)
        )
        assert ok is False and "trigger" in reason.lower()


class TestScoring:
    def _meta(self):
        return {
            "task_kind": "migrate-database",
            "tools": ["terminal", "file_read"],
            "intent_signals": ["migrate database"],
        }

    def test_full_match_scores_one(self):
        state = {
            "task_kind": "Migrate-Database",
            "tools": ["terminal", "file_read"],
            "goal": "please migrate database now",
        }
        assert score_trigger(state, self._meta()) == 1.0

    def test_no_match_scores_zero(self):
        state = {"task_kind": "write-poem", "tools": [], "goal": "haiku about rain"}
        assert score_trigger(state, self._meta()) == 0.0

    def test_best_matches_threshold_and_order(self):
        other = {"task_kind": "other", "tools": [], "intent_signals": []}
        partial = {"task_kind": "migrate-database", "tools": ["terminal"], "goal": ""}
        hits = best_matches(partial, [("a", self._meta()), ("b", other)], threshold=0.5)
        assert [h[0] for h in hits] == ["a"]
