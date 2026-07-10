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


class TestHandleSearchFilesGlobRedirect:
    """Integration tests for the _handle_search_files handler redirect."""

    def test_glob_in_content_mode_returns_redirect_error(self):
        """A glob pattern in content mode returns a helpful error, not a parse error."""
        result = _handle_search_files(
            {"pattern": "*.py", "target": "content"},
            task_id="test",
        )
        data = json.loads(result)
        assert "error" in data
        assert "glob" in data["error"].lower()
        assert "target='files'" in data["error"]
        # The error should suggest the exact fix
        assert "file_glob" in data["error"]

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

    def test_grep_alias_triggers_redirect(self):
        """The 'grep' alias for 'content' should also trigger the redirect."""
        result = _handle_search_files(
            {"pattern": "*.json", "target": "grep"},
            task_id="test",
        )
        data = json.loads(result)
        assert "error" in data
        assert "glob" in data["error"].lower()