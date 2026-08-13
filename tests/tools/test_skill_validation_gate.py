"""Tests for the pre-commit validation gate (#2189).

Auto-created (background-review-origin) skills must pass structural
validation BEFORE being admitted to the active library.  A failing skill is
NOT marked agent-created and its directory is rolled back.
"""

import json
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

from tools.skill_manager_tool import (
    skill_manage,
    validate_skill_content,
)
from tools.skill_provenance import (
    BACKGROUND_REVIEW,
    add_provenance_entry,
    init_source_chain,
    reset_current_write_origin,
    reset_source_chain,
    set_current_write_origin,
)


@contextmanager
def _skill_dir(tmp_path):
    """Patch SKILLS_DIR + get_all_skills_dirs so _find_skill uses tmp_path."""
    with (
        patch("tools.skill_manager_tool.SKILLS_DIR", tmp_path),
        patch("agent.skill_utils.get_all_skills_dirs", return_value=[tmp_path]),
    ):
        yield


# A structurally valid skill (passes the validation gate).
VALID_SKILL = """\
---
name: good-auto-skill
description: Use when analyzing repo structure for refactoring.
---

# Good Auto Skill

Step 1: Read the repository map to understand module dependencies.
Step 2: Identify circular imports and god-files.
Step 3: Propose a decomposition into focused modules.
"""

# Too short — body below minimum.
SHORT_BODY_SKILL = """\
---
name: short-skill
description: Use when doing something specific for routing.
---

# Short

Do it.
"""

# Placeholder body.
PLACEHOLDER_SKILL = """\
---
name: placeholder-skill
description: Use when processing user requests automatically.
---

# Placeholder Skill

TODO: write the actual instructions here.
This is a placeholder for now and will be filled in later.
"""

# Description too short (single word).
SHORT_DESC_SKILL = """\
---
name: short-desc
description: Helper
---

# Short Desc Skill

This skill has enough body content to pass the length check but the
description is only a single word, which is not a useful trigger phrase.
"""

# --- validate_skill_content (unit) -----------------------------------------


class TestValidateSkillContent:
    def test_valid_skill_passes(self, tmp_path):
        with _skill_dir(tmp_path):
            skill_manage("create", "good-auto-skill", content=VALID_SKILL)
            assert validate_skill_content("good-auto-skill") is None

    def test_short_body_fails(self, tmp_path):
        with _skill_dir(tmp_path):
            skill_manage("create", "short-skill", content=SHORT_BODY_SKILL)
            err = validate_skill_content("short-skill")
            assert err is not None
            assert "too short" in err.lower()

    def test_placeholder_body_fails(self, tmp_path):
        with _skill_dir(tmp_path):
            skill_manage("create", "placeholder-skill", content=PLACEHOLDER_SKILL)
            err = validate_skill_content("placeholder-skill")
            assert err is not None
            assert "placeholder" in err.lower() or "todo" in err.lower()

    def test_short_description_fails(self, tmp_path):
        with _skill_dir(tmp_path):
            skill_manage("create", "short-desc", content=SHORT_DESC_SKILL)
            err = validate_skill_content("short-desc")
            assert err is not None
            assert "description" in err.lower()

    def test_nonexistent_skill_returns_error(self, tmp_path):
        with _skill_dir(tmp_path):
            err = validate_skill_content("does-not-exist")
            assert err is not None
            assert "not found" in err.lower()


# --- Integration: validation gate blocks admission (#2189) -----------------


class TestValidationGateBlocksAdmission:
    """The validation gate must BLOCK admission of failing auto-created skills:
    the skill is NOT marked agent-created and its directory is rolled back."""

    def test_background_review_create_valid_skill_admitted(self, tmp_path):
        """A valid auto-created skill IS admitted (marked agent-created)."""
        token = set_current_write_origin(BACKGROUND_REVIEW)
        chain_token = init_source_chain()
        try:
            # Simulate a trusted source entry that a real background-review
            # fork would record (e.g. via read_file / terminal). The
            # provenance gate (#2288) requires at least one trusted source.
            add_provenance_entry("read_file", str(tmp_path / "research.md"))
            with _skill_dir(tmp_path):
                raw = skill_manage("create", "good-auto-skill", content=VALID_SKILL)
                result = json.loads(raw)
                assert result["success"] is True, result
                # Skill dir persists.
                assert (tmp_path / "good-auto-skill" / "SKILL.md").exists()
                # Marked agent-created.
                from tools.skill_usage import get_record

                rec = get_record("good-auto-skill")
                assert rec.get("created_by") is not None
        finally:
            reset_source_chain(chain_token)
            reset_current_write_origin(token)

    def test_background_review_create_short_skill_blocked(self, tmp_path):
        """A short-body auto-created skill is BLOCKED — not admitted, rolled back."""
        token = set_current_write_origin(BACKGROUND_REVIEW)
        try:
            with _skill_dir(tmp_path):
                raw = skill_manage("create", "short-skill", content=SHORT_BODY_SKILL)
                result = json.loads(raw)
                assert result["success"] is False, result
                assert "validation gate" in result["error"].lower()
                # Skill dir was rolled back.
                assert not (tmp_path / "short-skill").exists()
                # NOT marked agent-created.
                from tools.skill_usage import get_record

                rec = get_record("short-skill")
                assert rec.get("created_by") in {None, "", False}
        finally:
            reset_current_write_origin(token)

    def test_background_review_create_placeholder_skill_blocked(self, tmp_path):
        """A placeholder auto-created skill is BLOCKED."""
        token = set_current_write_origin(BACKGROUND_REVIEW)
        try:
            with _skill_dir(tmp_path):
                raw = skill_manage(
                    "create", "placeholder-skill", content=PLACEHOLDER_SKILL
                )
                result = json.loads(raw)
                assert result["success"] is False
                assert "validation gate" in result["error"].lower()
                assert not (tmp_path / "placeholder-skill").exists()
        finally:
            reset_current_write_origin(token)

    def test_foreground_create_short_skill_not_gated(self, tmp_path):
        """Foreground (user-directed) creates are NOT gated — only background-review."""
        with _skill_dir(tmp_path):
            raw = skill_manage("create", "short-skill", content=SHORT_BODY_SKILL)
            result = json.loads(raw)
            # Foreground create succeeds — validation gate does not apply.
            assert result["success"] is True, result
            assert (tmp_path / "short-skill" / "SKILL.md").exists()
