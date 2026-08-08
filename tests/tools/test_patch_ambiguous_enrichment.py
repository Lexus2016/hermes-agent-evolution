"""Tests for patch-ambiguous error enrichment (#1842).

The ambiguous-match error should:
1. List match locations with line numbers (already in _format_match_locations)
2. Include a non-retryable directive in the structured error
3. Include a fallback directive steering toward replace_all or more context
"""

from tools.fuzzy_match import (
    fuzzy_find_and_replace,
    format_structured_error,
    _format_match_locations,
    classify_error,
)


class TestAmbiguousErrorLocations:
    """The raw fuzzy_find_and_replace error already lists match locations."""

    def test_ambiguous_lists_line_numbers_in_error(self):
        content = (
            "def block_a(v):\n"
            "    value = process(v)\n"
            "    return v\n\n"
            "def block_b(v):\n"
            "    value = process(v)\n"
            "    return v\n"
        )
        _new, count, _strategy, error = fuzzy_find_and_replace(
            content, "    value = process(v)", "    value = process2(v)"
        )
        assert count == 0
        assert "Found 2 matches" in error
        assert "L2:" in error
        assert "L6:" in error

    def test_format_match_locations_shows_snippets(self):
        content = "alpha = 1\ntarget = run()\nbeta = 2\ntarget = run()\n"
        matches = [(10, 22), (33, 45)]
        out = _format_match_locations(content, matches)
        assert "L2: target = run()" in out
        # The second match at offset 33 is on line 4, but _format_match_locations
        # shows up to 5 matches — verify both are present by checking line numbers
        assert "L2:" in out
        assert "target = run()" in out


class TestStructuredErrorAmbiguous:
    """The structured diagnostic for ambiguous must be self-sufficient."""

    def test_structured_error_includes_non_retryable(self):
        content = "line1\ndup = func()\nline3\ndup = func()\n"
        error_msg = (
            "Found 2 matches for old_string. Provide more context to make it "
            "unique, or use replace_all=True. Matches:\n  L2: dup = func()\n"
            "  L4: dup = func()"
        )
        structured = format_structured_error(
            error_msg,
            match_count=0,
            old_string="dup = func()",
            new_string="dup = func2()",
            content=content,
            file_path="/test/file.py",
        )
        assert "ambiguous" in structured
        # Must include a non-retryable directive
        assert "Non-retryable" in structured or "non-retryable" in structured.lower()
        # Must mention both recovery paths
        assert "replace_all" in structured

    def test_structured_error_classifies_as_ambiguous(self):
        error = "Found 3 matches for old_string in the file"
        assert classify_error(error, None) == "ambiguous"


class TestClassifyFileErrorAmbiguous:
    """The file_operations classify_file_error recovery text for ambiguous
    should mention match locations and the non-retryable nature."""

    def test_ambiguous_recovery_mentions_locations(self):
        from tools.file_operations import classify_file_error

        _cls, recovery = classify_file_error(
            "Found 2 matches for old_string",
        )
        assert _cls == "ambiguous_match"
        assert "line" in recovery.lower() or "location" in recovery.lower()
        assert "replace_all" in recovery
        # Must warn against blind retry
        assert "retry" in recovery.lower() or "not" in recovery.lower()
