"""Tests for search_files/terminal parse_error classification (#1942, #1944).

Before this change, search_files regex/glob parse errors (e.g.
"rg: regex parse error: unclosed group") were classified as ``runtime_error``
by the generic "error:" catch-all rule in tool_diagnostics._RULES.
``runtime_error`` is retryable, so the loop_guard's non-retryable fast-path
never fired, and the agent spiraled on the same broken pattern.

This test verifies:
  1. Regex parse errors from search_files → ``parse_error`` (not runtime_error)
  2. ``parse_error`` is non-retryable for both ``search_files`` and ``terminal``
  3. Normal search failures (empty results, exit code 2 from grep) still
     classify correctly — NOT as parse_error (would over-block legitimate
     retryable searches).
"""

import pytest
from agent.tool_diagnostics import classify
from agent.loop_guard import _is_non_retryable, _NON_RETRYABLE_BY_TOOL


class TestSearchFilesParseErrorClassification:
    """#1944 — search_files regex/glob parse errors → parse_error, not runtime_error."""

    @pytest.mark.parametrize(
        "message",
        [
            "Search failed: rg: regex parse error: unclosed character class",
            "rg: regex parse error, invalid repetition operator",
            "grep: Invalid regular expression",
            "Search failed: error: unclosed parenthesis",
            "Search failed: error: unbalanced group",
            "rg: unrecognized repeat operator",
        ],
    )
    def test_regex_parse_error_classified_as_parse_error(self, message):
        """Each regex/glob failure message must classify as parse_error."""
        result = classify(message)
        assert result is not None, f"Expected classification for: {message}"
        assert result[0] == "parse_error", (
            f"Expected parse_error for {message!r}, got {result[0]}"
        )

    def test_parse_error_recovery_hint_mentions_fix(self):
        """The recovery hint should guide the agent to fix the pattern."""
        result = classify("rg: regex parse error: unclosed group")
        assert result is not None
        _category, hint = result
        assert "pattern" in hint.lower() or "regex" in hint.lower()

    def test_normal_search_failure_not_parse_error(self):
        """A generic exit-code-2 from grep/search must NOT be parse_error.

        Grep uses exit code 2 for file-not-found and other non-syntax errors.
        These are legitimately retryable with a different query/path.
        """
        result = classify("exit code 2")
        if result is not None:
            assert result[0] != "parse_error", (
                "Generic exit-code-2 should not classify as parse_error"
            )

    def test_empty_search_result_not_parse_error(self):
        """A 'no matches found' result is not_found, not parse_error."""
        result = classify("no matches found")
        if result is not None:
            assert result[0] != "parse_error"


class TestSearchFilesNonRetryable:
    """#1944 — search_files parse_error is non-retryable (spiral-bounded at 2)."""

    def test_search_files_parse_error_is_non_retryable(self):
        assert _is_non_retryable("search_files", "parse_error") is True

    def test_search_files_not_found_is_retryable(self):
        """Empty results are legitimately retryable with a broader query."""
        assert _is_non_retryable("search_files", "not_found") is False

    def test_search_files_runtime_error_is_retryable(self):
        """Generic runtime errors on search_files are NOT auto-non-retryable."""
        assert _is_non_retryable("search_files", "runtime_error") is False

    def test_search_files_in_non_retryable_registry(self):
        assert "search_files" in _NON_RETRYABLE_BY_TOOL
        assert "parse_error" in _NON_RETRYABLE_BY_TOOL["search_files"]


class TestTerminalParseErrorNonRetryable:
    """#1942 — terminal parse_error is non-retryable."""

    def test_terminal_parse_error_is_non_retryable(self):
        assert _is_non_retryable("terminal", "parse_error") is True

    def test_terminal_runtime_error_is_retryable(self):
        """Generic runtime errors on terminal are NOT auto-non-retryable."""
        assert _is_non_retryable("terminal", "runtime_error") is False

    def test_terminal_in_non_retryable_registry(self):
        assert "terminal" in _NON_RETRYABLE_BY_TOOL
        assert "parse_error" in _NON_RETRYABLE_BY_TOOL["terminal"]


class TestPatchAmbiguousStillWorks:
    """#1943 — ensure the existing patch ambiguous classification still works."""

    def test_patch_ambiguous_is_non_retryable(self):
        assert _is_non_retryable("patch", "ambiguous") is True

    def test_patch_ambiguous_classified(self):
        result = classify("Found 3 matches for old_string")
        assert result is not None
        assert result[0] == "ambiguous"
