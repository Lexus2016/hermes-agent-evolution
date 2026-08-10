"""Tests for skill compliance instrumentation (#2183)."""

from contextlib import contextmanager
from unittest.mock import patch

import pytest

from tools.skill_compliance import (
    check_boundary_violations,
    record_compliance,
    quality_summary,
    reset_compliance,
    set_active_skill,
    get_active_skill,
    record_tool_call_for_active_skill,
    get_active_skill_tool_calls,
    _read_forbidden_tools,
)

SKILL_WITH_FORBIDDEN = """\
---
name: restricted-skill
description: Use when doing restricted operations safely.
metadata:
  hermes:
    forbidden_tools:
      - terminal
      - write_file
---
# Restricted Skill
Do the task without using terminal or write_file.
"""

SKILL_NO_FORBIDDEN = """\
---
name: open-skill
description: Use when doing open operations freely.
---
# Open Skill
Do the task with any tools.
"""


@pytest.fixture(autouse=True)
def _clean():
    reset_compliance()
    set_active_skill(None)
    yield
    reset_compliance()
    set_active_skill(None)


@contextmanager
def _skill_dir(tmp_path):
    with patch("tools.skill_manager_tool.SKILLS_DIR", tmp_path), \
         patch("agent.skill_utils.get_all_skills_dirs", return_value=[tmp_path]):
        yield


class TestReadForbiddenTools:
    def test_reads_forbidden_from_frontmatter(self, tmp_path):
        with _skill_dir(tmp_path):
            from tools.skill_manager_tool import skill_manage
            skill_manage("create", "restricted-skill", content=SKILL_WITH_FORBIDDEN)
            assert _read_forbidden_tools("restricted-skill") == {"terminal", "write_file"}

    def test_empty_when_none_declared(self, tmp_path):
        with _skill_dir(tmp_path):
            from tools.skill_manager_tool import skill_manage
            skill_manage("create", "open-skill", content=SKILL_NO_FORBIDDEN)
            assert _read_forbidden_tools("open-skill") == set()

    def test_empty_for_nonexistent(self):
        assert _read_forbidden_tools("nope") == set()


class TestCheckBoundaryViolations:
    def test_detects_violation(self, tmp_path):
        with _skill_dir(tmp_path):
            from tools.skill_manager_tool import skill_manage
            skill_manage("create", "restricted-skill", content=SKILL_WITH_FORBIDDEN)
            assert check_boundary_violations("restricted-skill", ["read_file", "terminal"]) is True

    def test_no_violation_all_allowed(self, tmp_path):
        with _skill_dir(tmp_path):
            from tools.skill_manager_tool import skill_manage
            skill_manage("create", "restricted-skill", content=SKILL_WITH_FORBIDDEN)
            assert check_boundary_violations("restricted-skill", ["read_file"]) is False

    def test_no_violation_no_forbidden(self, tmp_path):
        with _skill_dir(tmp_path):
            from tools.skill_manager_tool import skill_manage
            skill_manage("create", "open-skill", content=SKILL_NO_FORBIDDEN)
            assert check_boundary_violations("open-skill", ["terminal"]) is False


class TestRecordCompliance:
    def test_records_trigger_comply(self):
        record_compliance("s", triggered=True, complied=True)
        s = quality_summary()["s"]
        assert s["trigger_count"] == 1 and s["comply_count"] == 1 and s["boundary_violation_count"] == 0

    def test_records_violation(self):
        record_compliance("s", triggered=True, complied=False, boundary_violated=True)
        assert quality_summary()["s"]["boundary_violation_count"] == 1

    def test_accumulates(self):
        record_compliance("s", triggered=True, complied=True)
        record_compliance("s", triggered=True, complied=False, boundary_violated=True)
        s = quality_summary()["s"]
        assert s["trigger_count"] == 2 and s["comply_count"] == 1 and s["boundary_violation_count"] == 1


class TestActiveSkillTracking:
    def test_set_get_active(self):
        set_active_skill("s")
        assert get_active_skill() == "s"

    def test_track_calls_when_active(self):
        set_active_skill("s")
        record_tool_call_for_active_skill("read_file")
        record_tool_call_for_active_skill("terminal")
        assert get_active_skill_tool_calls() == ["read_file", "terminal"]

    def test_noop_when_inactive(self):
        record_tool_call_for_active_skill("read_file")
        assert get_active_skill_tool_calls() == []

    def test_reset_on_new_skill(self):
        set_active_skill("a")
        record_tool_call_for_active_skill("read_file")
        set_active_skill("b")
        assert get_active_skill_tool_calls() == []


class TestEndToEnd:
    def test_violation_detected_and_recorded(self, tmp_path):
        with _skill_dir(tmp_path):
            from tools.skill_manager_tool import skill_manage
            skill_manage("create", "restricted-skill", content=SKILL_WITH_FORBIDDEN)
            set_active_skill("restricted-skill")
            record_tool_call_for_active_skill("read_file")
            record_tool_call_for_active_skill("terminal")
            violated = check_boundary_violations("restricted-skill", get_active_skill_tool_calls())
            record_compliance("restricted-skill", triggered=True, complied=not violated, boundary_violated=violated)
            s = quality_summary()["restricted-skill"]
            assert s["boundary_violation_count"] == 1 and s["comply_count"] == 0
