"""Tests for patch not_found classification fix (#1943).

The dominant patch no-match message from fuzzy_match.py — "Could not find a
match for old_string in the file" — was NOT classified by any rule in
tool_diagnostics._RULES. The not_found rule matched "not found" (with 'd'),
not "could not find". The unclassified error fell through to None, so
loop_guard never saw it as non-retryable, allowing the spiral to continue
until the generic fail threshold kicked in.
"""

import pytest
from agent.tool_diagnostics import classify
from agent.loop_guard import _is_non_retryable


class TestPatchNotFoundClassification:
    """#1943 — patch 'Could not find match' errors → not_found (non-retryable)."""

    @pytest.mark.parametrize(
        "message",
        [
            "Could not find match for old_string in /path/to/file.py",
            "Could not find a match for old_string in the file",
            "old_string not found in file",
            "0 matches in visible files",
            "no matches found for old_string",
        ],
    )
    def test_patch_no_match_classified_as_not_found(self, message):
        """Patch no-match messages must classify as not_found."""
        result = classify(message)
        assert result is not None, f"Expected classification for: {message}"
        assert result[0] == "not_found", (
            f"Expected not_found for {message!r}, got {result[0]}"
        )

    def test_patch_not_found_is_non_retryable(self):
        assert _is_non_retryable("patch", "not_found") is True

    def test_patch_ambiguous_still_works(self):
        """Ensure the existing ambiguous classification is not broken."""
        result = classify("Found 3 matches for old_string")
        assert result is not None
        assert result[0] == "ambiguous"
        assert _is_non_retryable("patch", "ambiguous") is True
