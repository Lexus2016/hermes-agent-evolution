"""Tests for search_files glob-as-regex detection (issue #887).

The model frequently passes shell glob patterns (e.g. ``*.py``) as the
regex ``pattern`` parameter in content-search mode, causing ripgrep
regex parse errors.  The fix detects globs and returns a helpful redirect
message instead of a cryptic parse error.
"""

import json
import pytest
from tools.file_tools import _looks_like_glob, _handle_search_files


class TestLooksLikeGlob:
    """Unit tests for the glob detection heuristic."""

    def test_simple_star_glob(self):
        assert _looks_like_glob("*.py") is True

    def test_star_prefix_glob(self):
        assert _looks_like_glob("*config*") is True

    def test_question_mark_glob(self):
        assert _looks_like_glob("config?.yml") is True

    def test_double_star_recursive_glob(self):
        assert _looks_like_glob("**/*.py") is True

    def test_escaped_star_is_not_glob(self):
        # \* is an escaped literal in regex — not a glob wildcard
        assert _looks_like_glob(r"\*\.py") is False

    def test_plain_regex_is_not_glob(self):
        assert _looks_like_glob("def foo") is False
        assert _looks_like_glob("search.*") is False  # . is a regex metachar, not a glob

    def test_empty_pattern(self):
        assert _looks_like_glob("") is False
        assert _looks_like_glob(None) is False

    def test_regex_char_class_not_flagged(self):
        # [a-z]+ is a regex, not a glob — no unescaped * or ?
        assert _looks_like_glob("[a-z]+") is False

    # --- #1484: lookaround / (?...) groups must NOT be flagged as globs ---

    def test_negative_lookahead_not_glob(self):
        # (?!...) is a regex negative lookahead — ? follows (, not a glob
        assert _looks_like_glob(r"(?!foo)bar") is False

    def test_positive_lookahead_not_glob(self):
        assert _looks_like_glob(r"(?=foo)bar") is False

    def test_negative_lookbehind_not_glob(self):
        # (?<=...) is the pattern that caused 59 retries/7d per #1484
        assert _looks_like_glob(r"(?<=foo)bar") is False

    def test_positive_lookbehind_not_glob(self):
        assert _looks_like_glob(r"(?<!foo)bar") is False

    def test_non_capturing_group_not_glob(self):
        # (?:...) is a non-capturing group — ? follows (, not a glob
        assert _looks_like_glob(r"(?:abc)+") is False

    def test_lookaround_with_quantifier_not_glob(self):
        # Combined lookaround + .* — still a regex, not a glob
        assert _looks_like_glob(r"(?<=\d).*") is False


class TestIsValidRegexShortCircuit:
    """The glob guard only matters for patterns that cause a *parse error*.

    A pattern that ``re.compile`` accepts cannot cause one, so
    ``_handle_search_files`` must let it through even when the
    ``_looks_like_glob`` heuristic flags it.  These are the
    search_files-glob-false-positive regression cases (12 occurrences across
    the introspection window).
    """

    @pytest.mark.parametrize("pattern", [
        # The heuristic sees ``*`` preceded by ``s`` (it does not track the
        # ``\s`` escape span) but the pattern compiles and is a legal regex.
        r'"verdict":\s*null',
        r'"head":\s*\{',
        r'def [a-z_]+\(self.*\).*:\s*$',
        r'"number":\s*\d{4}',
        r'"verdict":\s*"consumed"',
    ])
    def test_false_positive_regex_passes_through(self, pattern):
        """Real-world regexes the heuristic wrongly flagged as globs.

        These compile cleanly and must reach the search backend, not the
        glob-redirect error.
        """
        result = _handle_search_files(
            {"pattern": pattern, "target": "content"},
            task_id="test",
        )
        # Whatever search_tool returns (results or a non-glob error), it must
        # NOT be the glob-redirect error.
        try:
            data = json.loads(result)
            if "error" in data:
                assert "glob" not in data["error"].lower(), (
                    f"Pattern {pattern!r} is a valid regex and must not be "
                    f"redirected as a glob"
                )
        except (json.JSONDecodeError, TypeError):
            pass  # non-JSON -> went through to search_tool, which is correct

    @pytest.mark.parametrize("pattern", [
        "*.py",
        "*config*",
        "**/*.py",
        "*.json",
    ])
    def test_uncompilable_glob_auto_converts(self, pattern):
        """Patterns that fail ``re.compile`` (real globs) are auto-converted
        to regex and dispatched to the search — NOT returned as an error
        (#1788).  The handler now calls ``_glob_to_regex`` and proceeds."""
        result = _handle_search_files(
            {"pattern": pattern, "target": "content"},
            task_id="test",
        )
        # The result is whatever search_tool returns (search results or an
        # environment/path error) — it must NOT be the old glob-redirect
        # error message.
        try:
            data = json.loads(result)
            if "error" in data:
                assert "glob" not in data["error"].lower(), (
                    f"{pattern!r} should be auto-converted, not returned as "
                    f"a glob error"
                )
        except (json.JSONDecodeError, TypeError):
            pass  # Non-JSON result is fine — means it went through to search


class TestHandleSearchFilesGlobRedirect:
    """Integration tests for the _handle_search_files handler redirect."""

    def test_glob_in_content_mode_auto_converts(self):
        """A glob pattern in content mode is auto-converted to regex and
        dispatched to the search — NOT returned as an error (#1788)."""
        result = _handle_search_files(
            {"pattern": "*.py", "target": "content"},
            task_id="test",
        )
        # Must NOT be the old glob redirect error
        try:
            data = json.loads(result)
            if "error" in data:
                assert "glob" not in data["error"].lower()
                assert "target='files'" not in data.get("error", "")
        except (json.JSONDecodeError, TypeError):
            pass  # Non-JSON result is fine — means it went through to search

    def test_glob_in_files_mode_passes_through(self):
        """A glob pattern in files mode should NOT be redirected (it's the correct usage)."""
        # We can't easily test the full search path without a real env,
        # but we can verify the handler doesn't return the redirect error.
        # The handler will call search_tool which may fail on path resolution,
        # but it should NOT return the glob redirect error.
        result = _handle_search_files(
            {"pattern": "*.py", "target": "files"},
            task_id="test",
        )
        # It should NOT be the glob redirect error — it should either be
        # search results or a different error (path not found, etc.)
        try:
            data = json.loads(result)
            if "error" in data:
                assert "glob" not in data["error"].lower(), \
                    "File-search mode should not trigger glob redirect"
        except (json.JSONDecodeError, TypeError):
            pass  # Non-JSON result is fine — means it went through to search_tool

    def test_regex_in_content_mode_passes_through(self):
        """A valid regex in content mode should NOT be redirected."""
        result = _handle_search_files(
            {"pattern": "def foo", "target": "content"},
            task_id="test",
        )
        try:
            data = json.loads(result)
            if "error" in data:
                assert "glob" not in data["error"].lower(), \
                    "Valid regex should not trigger glob redirect"
        except (json.JSONDecodeError, TypeError):
            pass

    def test_glob_with_file_glob_already_set_passes_through(self):
        """If file_glob is already set, the pattern is probably a regex — don't redirect."""
        result = _handle_search_files(
            {"pattern": "*.py", "target": "content", "file_glob": "*.ts"},
            task_id="test",
        )
        try:
            data = json.loads(result)
            if "error" in data:
                assert "glob" not in data["error"].lower(), \
                    "Pattern with file_glob set should not trigger glob redirect"
        except (json.JSONDecodeError, TypeError):
            pass

    def test_grep_alias_auto_converts(self):
        """The 'grep' alias for 'content' should also auto-convert globs."""
        result = _handle_search_files(
            {"pattern": "*.json", "target": "grep"},
            task_id="test",
        )
        # Must NOT be the old glob redirect error
        try:
            data = json.loads(result)
            if "error" in data:
                assert "glob" not in data["error"].lower()
        except (json.JSONDecodeError, TypeError):
            pass


class TestInvalidRegexEnrichment:
    """#1588 — invalid regex patterns (not globs) must return an enriched
    error with the compile-failure reason, not a bare ripgrep parse error."""

    def test_unclosed_bracket_returns_reason(self):
        """An unclosed character class is an invalid regex, not a glob."""
        result = _handle_search_files(
            {"pattern": "[unclosed", "target": "content"},
            task_id="test",
        )
        data = json.loads(result)
        assert "error" in data
        assert "Invalid regex" in data["error"]
        # The specific re.error reason should be surfaced
        assert "unterminated" in data["error"].lower()

    def test_invalid_regex_mentions_escape_hint(self):
        """The error should hint at escaping metacharacters."""
        result = _handle_search_files(
            {"pattern": "(", "target": "content"},
            task_id="test",
        )
        data = json.loads(result)
        assert "error" in data
        assert "escape" in data["error"].lower()

    def test_valid_regex_not_caught_by_enrichment(self):
        """A valid regex must pass through to search_tool, not hit enrichment."""
        result = _handle_search_files(
            {"pattern": r"\bfoo\b", "target": "content"},
            task_id="test",
        )
        # Should NOT contain the enrichment error
        try:
            data = json.loads(result)
            if "error" in data:
                assert "Invalid regex" not in data["error"]
        except (json.JSONDecodeError, TypeError):
            pass  # non-JSON -> went through to search_tool, correct

    def test_invalid_regex_with_file_glob_passes_through(self):
        """When file_glob is set, an invalid regex pattern should pass through
        (the caller may intend a literal string combined with a filename filter)."""
        result = _handle_search_files(
            {"pattern": "(", "target": "content", "file_glob": "*.py"},
            task_id="test",
        )
        try:
            data = json.loads(result)
            if "error" in data:
                assert "Invalid regex" not in data["error"]
        except (json.JSONDecodeError, TypeError):
            pass


class TestStructuredRegexErrorReasons:
    """#2308 — search_files parse errors carry a structured reason + recovery
    directive so the agent fixes the pattern instead of blind-retrying with
    a near-identical one (74/7d, 17-deep spirals)."""

    def test_unclosed_bracket_has_invalid_regex_syntax_reason(self):
        result = _handle_search_files(
            {"pattern": "[unclosed", "target": "content"},
            task_id="test",
        )
        data = json.loads(result)
        assert data.get("reason") == "invalid_regex_syntax"
        assert "recovery" in data
        assert "malformed" in data["recovery"].lower()

    def test_unclosed_group_has_invalid_regex_syntax_reason(self):
        result = _handle_search_files(
            {"pattern": "(", "target": "content"},
            task_id="test",
        )
        data = json.loads(result)
        assert data.get("reason") == "invalid_regex_syntax"
        assert "recovery" in data

    def test_glob_negation_has_glob_as_regex_reason(self):
        """A '[!' glob negation is invalid regex — classify as glob_as_regex."""
        result = _handle_search_files(
            {"pattern": "[!a-z", "target": "content"},
            task_id="test",
        )
        data = json.loads(result)
        assert data.get("reason") == "glob_as_regex"
        assert "target='files'" in data["recovery"] or "file_glob" in data["recovery"]

    def test_lookbehind_has_unsupported_feature_reason(self):
        """A variable-width lookbehind is rejected by Python re — classify
        as unsupported_feature (ripgrep rejects it too)."""
        result = _handle_search_files(
            {"pattern": r"(?<=a+)b", "target": "content"},
            task_id="test",
        )
        data = json.loads(result)
        assert data.get("reason") == "unsupported_feature"
        assert "recovery" in data

    def test_valid_regex_has_no_reason(self):
        """A valid regex must pass through to search_tool, not hit the
        structured-error path."""
        result = _handle_search_files(
            {"pattern": r"\bfoo\b", "target": "content"},
            task_id="test",
        )
        try:
            data = json.loads(result)
            if "error" in data:
                assert "reason" not in data
        except (json.JSONDecodeError, TypeError):
            pass  # non-JSON -> went through to search_tool, correct