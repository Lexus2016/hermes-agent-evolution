"""Tests for tools/skill_admission_gate.py — pre-commit validation gate (#2181 Slice A).

Tests cover:
  - Config gate (off by default → always admitted)
  - Foreground exemption (non-background-review → always admitted)
  - Structural checks: frontmatter, description quality, body substance,
    circular self-reference
  - Admission verdict recording
  - Integration with _create_skill (roll-back on block)
"""

import json
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

from tools.skill_admission_gate import (
    AdmissionVerdict,
    ADMITTED,
    BLOCKED,
    validate_before_admission,
    _check_frontmatter,
    _check_description_quality,
    _check_body_substance,
    _check_no_self_reference,
    _split_frontmatter,
    _pre_commit_validation_enabled,
)


# -- Helpers ---------------------------------------------------------------

VALID_CONTENT = """\
---
name: my-skill
description: A well-formed skill that does something useful for the agent
category: testing
---
# My Skill

## Steps
1. First do this step
2. Then do that step
3. Finally verify the result

This is a real procedure with enough body text to pass the depth check.
It has multiple non-empty lines and a meaningful description.
"""

SHALLOW_CONTENT = """\
---
name: bad-skill
description: x
---
ok
"""


def _enable_gate():
    """Patch config + provenance so the gate is active for background-review."""
    return (
        patch(
            "tools.skill_admission_gate._pre_commit_validation_enabled",
            return_value=True,
        ),
        patch(
            "tools.skill_admission_gate.is_background_review",
            create=True,
            return_value=True,
        ),
    )


# -- Config gate tests -----------------------------------------------------


class TestConfigGate:
    def test_disabled_returns_admitted(self, tmp_path):
        """When config is off, the gate is a no-op (always admitted)."""
        with (
            patch.object(
                "tools.skill_admission_gate._pre_commit_validation_enabled",
                return_value=False,
                create=True,
            )
            if False
            else patch(
                "tools.skill_admission_gate._pre_commit_validation_enabled",
                return_value=False,
            )
        ):
            v = validate_before_admission("test", tmp_path, VALID_CONTENT)
        assert v.verdict == ADMITTED
        assert not v.blocked

    def test_foreground_exempt(self, tmp_path):
        """Foreground skills are always admitted even with the gate on."""
        with (
            patch(
                "tools.skill_admission_gate._pre_commit_validation_enabled",
                return_value=True,
            ),
            patch("tools.skill_provenance.is_background_review", return_value=False),
        ):
            v = validate_before_admission("test", tmp_path, SHALLOW_CONTENT)
        assert v.verdict == ADMITTED


# -- Structural check tests ------------------------------------------------


class TestFrontmatterCheck:
    def test_complete_frontmatter_passes(self):
        chk = _check_frontmatter({"name": "x", "description": "d"})
        assert chk[1] is True

    def test_missing_name_fails(self):
        chk = _check_frontmatter({"description": "d"})
        assert chk[1] is False
        assert "name" in chk[2]

    def test_missing_description_fails(self):
        chk = _check_frontmatter({"name": "x"})
        assert chk[1] is False
        assert "description" in chk[2]

    def test_empty_values_fail(self):
        chk = _check_frontmatter({"name": "", "description": ""})
        assert chk[1] is False


class TestDescriptionQuality:
    def test_good_description_passes(self):
        chk = _check_description_quality("A comprehensive skill for data analysis")
        assert chk[1] is True

    def test_too_short_fails(self):
        chk = _check_description_quality("short")
        assert chk[1] is False
        assert "too short" in chk[2]

    def test_placeholder_fails(self):
        chk = _check_description_quality("TODO: write description here")
        assert chk[1] is False
        assert "placeholder" in chk[2].lower()


class TestBodySubstance:
    def test_substantive_body_passes(self):
        body = "\n".join(f"Line {i} of content" for i in range(20))
        chk = _check_body_substance(body)
        assert chk[1] is True

    def test_trivial_body_fails(self):
        chk = _check_body_substance("ok")
        assert chk[1] is False

    def test_short_body_fails(self):
        body = "\n".join("a" for _ in range(3))
        chk = _check_body_substance(body)
        assert chk[1] is False


class TestCircularReference:
    def test_normal_skill_passes(self):
        content = VALID_CONTENT.replace("my-skill", "loop-check")
        chk = _check_no_self_reference("loop-check", content)
        assert chk[1] is True

    def test_circular_detected(self):
        """A short skill that only says 'use circular-skill' 3+ times."""
        content = """\
---
name: circular-skill
description: A skill that references itself too much for its size
---
Use circular-skill to run circular-skill then call circular-skill again.
"""
        chk = _check_no_self_reference("circular-skill", content)
        assert chk[1] is False
        assert "circular" in chk[2].lower()


# -- Integration: validate_before_admission --------------------------------


class TestValidateBeforeAdmission:
    def test_valid_skill_admitted(self, tmp_path):
        with (
            patch(
                "tools.skill_admission_gate._pre_commit_validation_enabled",
                return_value=True,
            ),
            patch("tools.skill_provenance.is_background_review", return_value=True),
            patch("tools.skill_admission_gate._record_admission"),
        ):
            v = validate_before_admission("my-skill", tmp_path, VALID_CONTENT)
        assert v.verdict == ADMITTED
        assert len(v.errors) == 0
        assert len(v.checks) >= 3

    def test_shallow_skill_blocked(self, tmp_path):
        with (
            patch(
                "tools.skill_admission_gate._pre_commit_validation_enabled",
                return_value=True,
            ),
            patch("tools.skill_provenance.is_background_review", return_value=True),
            patch("tools.skill_admission_gate._record_admission"),
        ):
            v = validate_before_admission("bad-skill", tmp_path, SHALLOW_CONTENT)
        assert v.verdict == BLOCKED
        assert v.blocked is True
        assert len(v.errors) > 0


class TestSplitFrontmatter:
    def test_parses_yaml_frontmatter(self):
        fm, body = _split_frontmatter(VALID_CONTENT)
        assert fm["name"] == "my-skill"
        assert "description" in fm
        assert "My Skill" in body

    def test_no_frontmatter(self):
        fm, body = _split_frontmatter("just plain text")
        assert fm == {}
        assert body == "just plain text"


# -- Integration with _create_skill ---------------------------------------


class TestCreateSkillIntegration:
    """End-to-end: the gate blocks a trivial background-review skill."""

    @contextmanager
    def _skill_dir(self, tmp_path):
        with (
            patch("tools.skill_manager_tool.SKILLS_DIR", tmp_path),
            patch("agent.skill_utils.get_all_skills_dirs", return_value=[tmp_path]),
        ):
            yield

    def test_background_review_trivial_skill_blocked(self, tmp_path):
        from tools import skill_manager_tool

        trivial = """\
---
name: trivial
description: x
---
ok
"""
        with (
            self._skill_dir(tmp_path),
            patch(
                "tools.skill_manager_tool.is_background_review",
                create=True,
                return_value=True,
            )
            if False
            else patch(
                "tools.skill_provenance.is_background_review", return_value=True
            ),
            patch(
                "tools.skill_admission_gate._pre_commit_validation_enabled",
                return_value=True,
            ),
            patch("tools.skill_admission_gate._record_admission"),
        ):
            result = skill_manager_tool._create_skill("trivial", trivial, category="")
        assert result["success"] is False
        assert "Pre-commit validation gate" in result["error"]

    def test_foreground_skill_not_blocked(self, tmp_path):
        """Foreground skills bypass the gate entirely."""
        from tools import skill_manager_tool

        trivial = """\
---
name: fg-trivial
description: x
---
ok
"""
        with (
            self._skill_dir(tmp_path),
            patch("tools.skill_provenance.is_background_review", return_value=False),
            patch(
                "tools.skill_admission_gate._pre_commit_validation_enabled",
                return_value=True,
            ),
        ):
            result = skill_manager_tool._create_skill(
                "fg-trivial", trivial, category=""
            )
        assert result["success"] is True
