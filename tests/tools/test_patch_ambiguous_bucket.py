"""Tests for decomposing the patch 'ambiguous_match' error bucket (#2354).

The ambiguous_match bucket recurs at 229/7d with 21-deep spirals because the
old recovery hint ("include more context or use replace_all") gave no anti-retry
signal — the model re-sent the same non-unique old_string, matching the same
multiple locations again. This test verifies the bucket is now decomposed into
actionable subclasses, each with a targeted recovery hint:

  - replace_all_intent: error mentions replace_all → steer to re-send with
    replace_all=True, with explicit anti-retry language.
  - ambiguous_insufficient_context: error mentions more context → steer to
    re-read and include surrounding lines.
  - ambiguous_not_unique: generic fallback → explicit "do NOT retry the same
    old_string" anti-retry language.

Child of #2334.
"""

from tools.file_operations import (
    PatchResult,
    classify_file_error,
)


class TestReplaceAllIntentClassification:
    """When the error message mentions replace_all, classify as
    replace_all_intent — the model likely WANTS all occurrences replaced."""

    def test_replace_all_in_error_classified(self):
        error = (
            "Found 3 matches for old_string at:\n"
            "  Line 10: def foo()\n  Line 25: def foo()\n"
            "Provide more context to make it unique, or use replace_all=True."
        )
        cls, hint = classify_file_error(error)
        assert cls == "replace_all_intent"
        assert "replace_all=True" in hint
        assert "do not retry" in hint.lower()

    def test_replace_all_anti_retry_language(self):
        """The hint must contain explicit anti-retry language so the model
        doesn't re-send the same call with replace_all unset."""
        error = "Found 2 matches for old_string — use replace_all to replace all"
        cls, hint = classify_file_error(error)
        assert cls == "replace_all_intent"
        assert "do not retry" in hint.lower()


class TestAmbiguousInsufficientContextClassification:
    """When the error mentions 'more context', classify as
    ambiguous_insufficient_context — steer to re-read and add context."""

    def test_more_context_in_error_classified(self):
        error = (
            "Found 5 matches for old_string. Include more context to make it unique."
        )
        cls, hint = classify_file_error(error)
        assert cls == "ambiguous_insufficient_context"
        assert "re-read" in hint.lower() or "read_file" in hint.lower()
        assert "do not retry" in hint.lower()

    def test_surrounding_context_variant(self):
        error = (
            "Found 2 matches for old_string at:\n"
            "  Line 5: x = 1\n"
            "Add surrounding context to disambiguate."
        )
        cls, hint = classify_file_error(error)
        assert cls == "ambiguous_insufficient_context"
        assert "read_file" in hint.lower() or "re-read" in hint.lower()

    def test_longer_old_string_variant(self):
        error = "Found 4 matches for old_string. Use a longer old_string."
        cls, _ = classify_file_error(error)
        assert cls == "ambiguous_insufficient_context"


class TestAmbiguousNotUniqueClassification:
    """When the error is a generic ambiguous-match message (no replace_all
    or context keywords), classify as ambiguous_not_unique with anti-retry."""

    def test_generic_ambiguous_classified(self):
        error = "Found 3 matches for old_string at:\n  Line 10: x\n  Line 20: x"
        cls, hint = classify_file_error(error)
        assert cls == "ambiguous_not_unique"
        assert "do not retry" in hint.lower()
        assert "replace_all" in hint.lower() or "context" in hint.lower()

    def test_anti_retry_language_present(self):
        """The recovery hint MUST contain anti-retry language — this is the
        core fix for the 21-deep spirals."""
        error = "Found 2 matches for old_string at:\n  Line 1: foo"
        cls, hint = classify_file_error(error)
        assert cls == "ambiguous_not_unique"
        assert "do not retry" in hint.lower()
        assert "same old_string" in hint.lower()


class TestPatchResultAmbiguousSubclasses:
    """Verify the subclasses propagate through PatchResult.to_dict()."""

    def test_patch_result_replace_all_intent(self):
        d = PatchResult(
            success=False,
            error=(
                "Found 3 matches for old_string at:\n  Line 10: x\n"
                "Use replace_all=True to replace all."
            ),
        ).to_dict()
        assert d["error_class"] == "replace_all_intent"
        assert "recovery" in d

    def test_patch_result_ambiguous_not_unique(self):
        d = PatchResult(
            success=False,
            error="Found 2 matches for old_string at:\n  Line 1: foo\n  Line 2: foo",
        ).to_dict()
        assert d["error_class"] == "ambiguous_not_unique"
        assert "recovery" in d


class TestNoRegression:
    """Existing classifications still work correctly alongside the new
    subclasses."""

    def test_permission_denied_still_works(self):
        cls, _ = classify_file_error("Permission denied")
        assert cls == "permission"

    def test_not_found_still_works(self):
        cls, _ = classify_file_error("File not found: /tmp/foo.txt")
        assert cls == "not_found"

    def test_fuzzy_match_still_works(self):
        """No-match errors still classify as fuzzy_match, not ambiguous."""
        cls, _ = classify_file_error(
            "Could not find a match for old_string in the file"
        )
        assert cls == "fuzzy_match"
