"""Tests for #1703: patch replace-mode + empty old_string spiral fix.

The agent confuses replace mode (needs non-empty old_string) with insertion.
Without the early gate, the error propagates without a mode=patch suggestion,
and the consecutive-failure tracker never fires — producing 5-8 retry spirals.
"""

import tempfile
from pathlib import Path
from unittest.mock import patch

from tools.file_tools import patch_tool


class TestPatchEmptyOldString1703:
    """The early validation gate catches empty-string old_string before it
    reaches fuzzy_match, and provides an actionable diagnostic."""

    def test_empty_old_string_mentions_mode_patch(self):
        """First occurrence: error message must suggest mode='patch' for insertions."""
        with tempfile.TemporaryDirectory() as tmpdir:
            f = Path(tmpdir) / "test.py"
            f.write_text("hello world\n")
            result = patch_tool(
                mode="replace",
                path=str(f),
                old_string="",
                new_string="inserted line\n",
                task_id="test-1703-first",
            )
            assert "empty" in result.lower()
            assert "mode='patch'" in result

    def test_empty_old_string_mentions_read_file(self):
        """Error message should direct the agent to read_file for current content."""
        with tempfile.TemporaryDirectory() as tmpdir:
            f = Path(tmpdir) / "test.py"
            f.write_text("hello world\n")
            result = patch_tool(
                mode="replace",
                path=str(f),
                old_string="",
                new_string="new\n",
                task_id="test-1703-readfile",
            )
            assert "read_file" in result

    def test_empty_old_string_non_retryable_after_2(self):
        """After 2 consecutive empty-old_string failures on the same file,
        the error must escalate to NON-RETRYABLE to break the spiral."""
        with tempfile.TemporaryDirectory() as tmpdir:
            f = Path(tmpdir) / "test.py"
            f.write_text("hello world\n")
            tid = "test-1703-escalation"
            # First empty attempt
            r1 = patch_tool(
                mode="replace", path=str(f), old_string="", new_string="x", task_id=tid
            )
            assert "NON-RETRYABLE" not in r1
            # Second empty attempt — should escalate
            r2 = patch_tool(
                mode="replace", path=str(f), old_string="", new_string="x", task_id=tid
            )
            assert "NON-RETRYABLE" in r2

    def test_none_old_string_still_gives_generic_error(self):
        """old_string=None (not empty string) should still get the original
        'old_string and new_string required' error, not the new empty-string path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            f = Path(tmpdir) / "test.py"
            f.write_text("hello\n")
            result = patch_tool(
                mode="replace",
                path=str(f),
                old_string=None,
                new_string="x",
                task_id="test-1703-none",
            )
            assert "required" in result

    def test_non_empty_old_string_proceeds_normally(self):
        """A valid non-empty old_string should NOT be intercepted by the new gate."""
        with tempfile.TemporaryDirectory() as tmpdir:
            f = Path(tmpdir) / "test.py"
            f.write_text("hello world\n")
            result = patch_tool(
                mode="replace",
                path=str(f),
                old_string="hello world",
                new_string="goodbye world",
                task_id="test-1703-valid",
            )
            # Should succeed, not return an error
            assert "empty" not in result.lower()
            assert "goodbye world" in f.read_text()
