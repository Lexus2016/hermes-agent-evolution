"""Tests for delegation pattern promotion and retrieval in experience bank (#2261)."""

import time
from pathlib import Path

import pytest

from agent.experience_bank import (
    DelegationPattern,
    delegation_patterns_path,
    find_matching_delegation_patterns,
    load_delegation_patterns,
    record_delegation_outcome,
    save_delegation_patterns,
)


@pytest.fixture
def temp_hermes_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


class TestDelegationExperienceBank:
    """Test delegation patterns asset bank."""

    def test_delegation_pattern_serialization(self):
        pat = DelegationPattern(
            task_type="code_review",
            role="Code Reviewer",
            model="anthropic/claude-3-5-sonnet",
            goal_template="Review PR diff and check security",
            context_keys=["diff", "pr_metadata"],
            success_count=3,
            total_count=4,
        )
        assert pat.success_rate == 0.75
        d = pat.to_dict()
        assert d["task_type"] == "code_review"
        assert d["role"] == "Code Reviewer"
        assert d["context_keys"] == ["diff", "pr_metadata"]

        loaded = DelegationPattern.from_dict(d)
        assert loaded.task_type == pat.task_type
        assert loaded.role == pat.role
        assert loaded.success_count == 3
        assert loaded.total_count == 4

    def test_record_and_retrieve_delegation_patterns(self, temp_hermes_home):
        # Initial state should be empty
        assert load_delegation_patterns() == []

        # Record successful delegation
        ok = record_delegation_outcome(
            task_type="research_task",
            role="Researcher",
            model="gemini-2.5-pro",
            goal_template="Search docs for {query}",
            context_keys=["topic", "query"],
            success=True,
        )
        assert ok is True

        patterns = load_delegation_patterns()
        assert len(patterns) == 1
        assert patterns[0].task_type == "research_task"
        assert patterns[0].role == "Researcher"
        assert patterns[0].success_count == 1
        assert patterns[0].total_count == 1
        assert patterns[0].success_rate == 1.0

        # Record another successful delegation for same pattern
        record_delegation_outcome(
            task_type="research_task",
            role="Researcher",
            success=True,
        )
        patterns = load_delegation_patterns()
        assert len(patterns) == 1
        assert patterns[0].total_count == 2
        assert patterns[0].success_count == 2

        # Record a failing delegation for a different pattern
        record_delegation_outcome(
            task_type="unstable_task",
            role="Tester",
            success=False,
        )
        patterns = load_delegation_patterns()
        assert len(patterns) == 2

        # Retrieve matching patterns
        matches = find_matching_delegation_patterns("research_task")
        assert len(matches) == 1
        assert matches[0].role == "Researcher"

        # Matching with low success rate filter excludes failing pattern
        unstable_matches = find_matching_delegation_patterns(
            "unstable_task", min_success_rate=0.5
        )
        assert len(unstable_matches) == 0

        # Fuzzy partial matching
        fuzzy_matches = find_matching_delegation_patterns("research")
        assert len(fuzzy_matches) == 1
        assert fuzzy_matches[0].task_type == "research_task"
