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
