"""Upstream synchronization must not widen autonomous skill-write ownership."""
import json
from unittest.mock import patch

import pytest

from tests.tools.test_skill_manager_tool import _skill_dir, VALID_SKILL_CONTENT
from tools.skill_manager_tool import skill_manage
from tools.skill_manager_guards import mark_background_review_skill_read
from tools.skill_provenance import BACKGROUND_REVIEW, set_current_write_origin, reset_current_write_origin


@pytest.mark.parametrize("protection", ["pinned", "bundled", "user_owned"])
@pytest.mark.parametrize("action", ["edit", "patch", "write_file", "remove_file", "delete"])
def test_read_protected_skill_does_not_grant_background_write(tmp_path, protection, action):
    directory = tmp_path / "kept-skill"
    directory.mkdir()
    main = directory / "SKILL.md"
    main.write_text(VALID_SKILL_CONTENT)
    supporting = directory / "notes.txt"
    supporting.write_text("keep this too")
    args = dict(action=action, name="kept-skill", content=VALID_SKILL_CONTENT.replace("Do the thing", "Changed"),
                old_string="Do the thing", new_string="Changed", file_path="notes.txt", file_content="Changed",
                absorbed_into="kept-skill")
    record = {"pinned": protection == "pinned", "created_by": "user" if protection == "user_owned" else "agent"}
    with _skill_dir(tmp_path), patch("tools.skill_usage.get_record", return_value=record), \
         patch("tools.skill_usage.load_usage", return_value={"kept-skill": record}), \
         patch("tools.skill_usage.is_bundled", return_value=protection == "bundled"), \
         patch("tools.skill_usage.is_hub_installed", return_value=False), \
         patch("tools.skill_usage.is_protected_builtin", return_value=False):
        token = set_current_write_origin(BACKGROUND_REVIEW)
        try:
            mark_background_review_skill_read(main)
            mark_background_review_skill_read(supporting)
            result = json.loads(skill_manage(**args))
        finally:
            reset_current_write_origin(token)
    assert result["success"] is False, result
    assert "Refusing background curator" in result["error"]
    assert main.read_text() == VALID_SKILL_CONTENT
    assert supporting.read_text() == "keep this too"
