# -*- coding: utf-8 -*-
"""Unit tests for Automatic Skill Crystallization (#2359)."""

from pathlib import Path
import pytest

from evolution.lib.skill_crystallizer import (
    CrystallizedSkillCandidate,
    SkillCrystallizer,
)


class TestSkillCrystallizer:
    """Test suite for skill crystallization and validation."""

    def test_candidate_serialization(self):
        candidate = CrystallizedSkillCandidate(
            name="docker-build-helper",
            description="Automated docker build workflow",
            skill_markdown="---\nname: docker-build-helper\ndescription: test\n---\n# Body",
            source_session_id="sess_123",
            reusability_score=0.85,
        )
        d = candidate.to_dict()
        assert d["name"] == "docker-build-helper"
        assert d["size_bytes"] > 0

        restored = CrystallizedSkillCandidate.from_dict(d)
        assert restored.name == candidate.name
        assert restored.source_session_id == candidate.source_session_id

    def test_reflect_on_successful_trace(self):
        trace = {
            "status": "success",
            "session_id": "sess_abc",
            "goal": "Migrate database schema and verify indexes",
            "tool_calls": [
                {"name": "file_read", "arguments": '{"path": "schema.sql"}'},
                {
                    "name": "terminal",
                    "arguments": '{"command": "alembic upgrade head"}',
                },
                {"name": "terminal", "arguments": '{"command": "pytest tests/db"}'},
            ],
        }
        candidate = SkillCrystallizer.reflect_on_trace(trace)
        assert candidate is not None
        assert "migrate-database" in candidate.name
        assert candidate.reusability_score >= 0.7
        assert "alembic upgrade head" not in candidate.name  # Name is sanitized
        assert (
            "schema.sql" in candidate.skill_markdown
            or "file_read" in candidate.skill_markdown
        )

    def test_ignore_failed_or_trivial_trace(self):
        # Failed trace
        failed_trace = {
            "status": "error",
            "tool_calls": [{"name": "terminal", "arguments": "{}"}],
        }
        assert SkillCrystallizer.reflect_on_trace(failed_trace) is None

        # Trivial trace (too few actions)
        trivial_trace = {
            "status": "success",
            "tool_calls": [{"name": "web_search", "arguments": "{}"}],
        }
        assert SkillCrystallizer.reflect_on_trace(trivial_trace, min_actions=2) is None

    def test_save_skill(self, tmp_path: Path):
        candidate = CrystallizedSkillCandidate(
            name="test-workflow",
            description="Test workflow description",
            skill_markdown="---\nname: test-workflow\ndescription: Test workflow description\n---\n# Test Workflow",
        )
        saved_path = SkillCrystallizer.save_skill(candidate, tmp_path)
        assert saved_path.exists()
        assert saved_path.name == "SKILL.md"
        assert saved_path.parent.name == "test-workflow"
        assert saved_path.read_text(encoding="utf-8") == candidate.skill_markdown


class TestEvidenceBar:
    """Memory->skill evidence bar (#2746)."""

    def _candidate(self, reusability: float = 0.8) -> CrystallizedSkillCandidate:
        return CrystallizedSkillCandidate(
            name="test",
            description="test",
            skill_markdown="---\nname: test\ndescription: test\n---\n# Body",
            reusability_score=reusability,
        )

    def test_meets_bar_when_all_clear(self):
        ok, reason = SkillCrystallizer.meets_evidence_bar(
            self._candidate(), distinct_tools=3, action_count=5, verified=True
        )
        assert ok is True
        assert "meets" in reason

    def test_fails_low_reusability(self):
        ok, reason = SkillCrystallizer.meets_evidence_bar(
            self._candidate(reusability=0.3), distinct_tools=3, action_count=5
        )
        assert ok is False
        assert "reusability" in reason

    def test_fails_single_tool(self):
        ok, reason = SkillCrystallizer.meets_evidence_bar(
            self._candidate(), distinct_tools=1, action_count=5
        )
        assert ok is False
        assert "distinct tool" in reason

    def test_fails_too_few_actions(self):
        ok, reason = SkillCrystallizer.meets_evidence_bar(
            self._candidate(), distinct_tools=3, action_count=2
        )
        assert ok is False
        assert "action" in reason

    def test_fails_unverified(self):
        ok, reason = SkillCrystallizer.meets_evidence_bar(
            self._candidate(), distinct_tools=3, action_count=5, verified=False
        )
        assert ok is False
        assert "verified" in reason

    def test_reflect_rejects_single_tool_trace(self):
        """A trace using only one tool must NOT be promoted to a skill."""
        trace = {
            "status": "success",
            "session_id": "sess_single",
            "goal": "Run a single command",
            "tool_calls": [
                {"name": "terminal", "arguments": '{"command": "ls"}'},
                {"name": "terminal", "arguments": '{"command": "pwd"}'},
                {"name": "terminal", "arguments": '{"command": "whoami"}'},
            ],
        }
        assert SkillCrystallizer.reflect_on_trace(trace) is None

    def test_reflect_rejects_unverified_status(self):
        """A trace with status 'ok' (not verified success) must NOT promote."""
        trace = {
            "status": "ok",
            "session_id": "sess_ok",
            "goal": "Multi-tool workflow",
            "tool_calls": [
                {"name": "file_read", "arguments": '{"path": "a"}'},
                {"name": "terminal", "arguments": '{"command": "x"}'},
                {"name": "web_search", "arguments": "{}"},
                {"name": "terminal", "arguments": '{"command": "y"}'},
            ],
        }
        assert SkillCrystallizer.reflect_on_trace(trace) is None
