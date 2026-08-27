"""Regression tests for the patch preflight guard (#3238)."""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parents[2]))

from tools.file_tools import patch_tool


class TestPatchPreflight:
    def test_empty_old_string_returns_re_read_action(self, tmp_path):
        target = tmp_path / "sample.py"
        target.write_text("print('hello')\n")

        result = patch_tool(
            mode="replace",
            path=str(target),
            old_string="",
            new_string="pass",
            task_id="test-empty",
        )

        payload = json.loads(result)
        assert payload["action"] == "re_read_file"
        assert payload["reason"] == "empty old_string"
        assert payload["path"] == str(target)
        assert payload["patch_preflight_blocked"] == 1
        assert payload["argument_shape_spiral"] is False
        assert "read_file" in payload["message"]

    def test_counter_increments_per_block(self, tmp_path):
        target = tmp_path / "sample.py"
        target.write_text("abc def ghi\n")
        for i in range(1, 5):
            result = patch_tool(
                mode="replace",
                path=str(target),
                old_string="",
                new_string="x",
                task_id="test-counter",
            )
            payload = json.loads(result)
            assert payload["patch_preflight_blocked"] == i
            assert payload["argument_shape_spiral"] is (i >= 3)

    def test_valid_old_string_is_not_blocked(self, tmp_path):
        target = tmp_path / "sample.py"
        target.write_text("def hello():\n    return 42\n")

        result = patch_tool(
            mode="replace",
            path=str(target),
            old_string="def hello():\n    return 42\n",
            new_string="def hello():\n    return 43\n",
            task_id="test-valid",
        )

        # Valid patch should succeed (returned as JSON with success flag).
        payload = json.loads(result)
        assert payload.get("success") is True
        assert target.read_text() == "def hello():\n    return 43\n"

    def test_short_old_string_still_allowed(self, tmp_path):
        """Short old_strings are deferred to the fuzzy matcher; not blocked."""
        target = tmp_path / "sample.py"
        target.write_text("x\n")

        result = patch_tool(
            mode="replace",
            path=str(target),
            old_string="x",
            new_string="y",
            task_id="test-short-allowed",
        )

        payload = json.loads(result)
        assert payload.get("success") is True
        assert target.read_text() == "y\n"

    def test_no_blind_retry(self, tmp_path):
        """Blocked preflight returns immediately, no file mutation."""
        original = "unchanged\n"
        target = tmp_path / "sample.py"
        target.write_text(original)

        patch_tool(
            mode="replace",
            path=str(target),
            old_string="",
            new_string="mutated",
            task_id="test-no-retry",
        )

        assert target.read_text() == original
