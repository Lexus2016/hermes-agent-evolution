"""Tests for decomposing the patch 'ambiguous_match' error bucket (#2354).

The bucket is split into 3 actionable subclasses, each with explicit
anti-retry language to break the 21-deep retry spirals (229 failures/7d).
Child of #2334.
"""

from tools.file_operations import PatchResult, classify_file_error


class TestReplaceAllIntent:
    """Error mentions replace_all → model likely wants all occurrences."""

    def test_replace_all_in_error(self):
        error = (
            "Found 3 matches for old_string:\n  Line 10: x\n"
            "Use replace_all=True to replace all."
        )
        cls, hint = classify_file_error(error)
        assert cls == "replace_all_intent"
        assert "replace_all" in hint
        assert "Do NOT" in hint


class TestInsufficientContext:
    """Error mentions more context → steer to re-read and add context."""

    def test_more_context_keyword(self):
        error = "Found 5 matches for old_string. Include more context."
        cls, hint = classify_file_error(error)
        assert cls == "ambiguous_insufficient_context"
        assert "read_file" in hint or "re-read" in hint
        assert "Do NOT" in hint

    def test_longer_keyword(self):
        error = "Found 4 matches for old_string. Use a longer old_string."
        cls, _ = classify_file_error(error)
        assert cls == "ambiguous_insufficient_context"


class TestNotUnique:
    """Generic ambiguous-match (no keywords) → explicit anti-retry."""

    def test_generic_ambiguous(self):
        error = "Found 3 matches for old_string at:\n  Line 10: x\n  Line 20: x"
        cls, hint = classify_file_error(error)
        assert cls == "ambiguous_not_unique"
        assert "Do NOT" in hint
        assert "same old_string" in hint


class TestPatchResultSubclasses:
    """Subclasses propagate through PatchResult.to_dict()."""

    def test_replace_all_intent_via_patch_result(self):
        d = PatchResult(
            success=False,
            error="Found 3 matches for old_string. Use replace_all=True.",
        ).to_dict()
        assert d["error_class"] == "replace_all_intent"

    def test_not_unique_via_patch_result(self):
        d = PatchResult(
            success=False,
            error="Found 2 matches for old_string at:\n  Line 1: foo\n  Line 2: foo",
        ).to_dict()
        assert d["error_class"] == "ambiguous_not_unique"


class TestNoRegression:
    """Existing classifications still work alongside the new subclasses."""

    def test_fuzzy_match_unchanged(self):
        cls, _ = classify_file_error(
            "Could not find a match for old_string in the file"
        )
        assert cls == "fuzzy_match"

    def test_permission_unchanged(self):
        cls, _ = classify_file_error("Permission denied")
        assert cls == "permission"
