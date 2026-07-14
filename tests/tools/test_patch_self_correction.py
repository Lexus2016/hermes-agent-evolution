"""Tests for structured error context in patch self-correction (issue #996).

The structured error package gives the model everything it needs to produce
a corrected patch on its next turn WITHOUT re-reading the file:

- Error type classification (no_match, ambiguous, identical, escape_drift)
- The closest matching region with line numbers (±5 lines)
- A recovery suggestion matched to the error type

This file tests the pure functions in ``tools.fuzzy_match`` — the functions
that build the structured error context.  Integration through the full
``_handle_patch`` pipeline is tested in ``test_patch_failure_tracking.py``.
"""

import json

import pytest

from tools.fuzzy_match import (
    classify_error,
    format_structured_error,
    _format_file_context_snippet,
    ERROR_TYPES,
)


class TestClassifyError:
    def test_no_match(self):
        assert classify_error("Could not find a match for old_string", None) == "no_match"

    def test_no_match_variant(self):
        assert classify_error("Could not find match for old_string in file.py", None) == "no_match"

    def test_ambiguous(self):
        assert classify_error("Found 3 matches for old_string", None) == "ambiguous"

    def test_identical_via_strategy(self):
        assert classify_error("some error", "identical") == "identical"

    def test_identical_via_text(self):
        assert classify_error("old_string and new_string are identical", None) == "identical"

    def test_escape_drift(self):
        assert classify_error("Escape-drift detected: backslash issue", None) == "escape_drift"

    def test_escape_drift_no_hyphen(self):
        assert classify_error("Escape drift detected in old_string", None) == "escape_drift"

    def test_permission(self):
        assert classify_error("Write denied: protected path", None) == "permission"

    def test_read_failed(self):
        assert classify_error("Failed to read file: /tmp/x.py", None) == "read_failed"

    def test_write_failed(self):
        assert classify_error("Failed to write changes to file", None) == "write_failed"

    def test_unknown(self):
        assert classify_error("something weird happened", None) == "unknown"

    def test_none_error(self):
        assert classify_error(None, None) == "unknown"


class TestFormatFileContextSnippet:
    def test_finds_closest_region(self):
        content = "line1\nline2\ndef foo():\n    return 1\nline5\nline6\n"
        old_string = "def foo():\n    return 2"
        snippet = _format_file_context_snippet(content, old_string, context_lines=2)
        assert "def foo" in snippet
        assert "return 1" in snippet
        # Line numbers should be present
        assert "   3" in snippet  # line 3 is def foo()

    def test_marks_best_match_line(self):
        content = "a\nb\ndef target():\n    pass\ne\nf\n"
        old_string = "def target():\n    pass"
        snippet = _format_file_context_snippet(content, old_string, context_lines=1)
        # The >> marker should be on the best-matching line
        assert ">>" in snippet

    def test_empty_for_no_similar_content(self):
        content = "completely different content here\n"
        old_string = "def totally_unrelated():\n    pass"
        snippet = _format_file_context_snippet(content, old_string)
        assert snippet == ""

    def test_empty_for_empty_inputs(self):
        assert _format_file_context_snippet("", "old") == ""
        assert _format_file_context_snippet("content", "") == ""

    def test_context_lines_respected(self):
        content = "\n".join(f"line{i}" for i in range(1, 21))
        old_string = "line10\nline11"
        snippet = _format_file_context_snippet(content, old_string, context_lines=3)
        # Should include lines 7 through ~14 (3 before line 10, 3 after the block)
        assert "line7" in snippet
        assert "line14" in snippet or "line13" in snippet
        # Should NOT include lines too far away — check by line number prefix,
        # not substring ("line1" is a substring of "line10" through "line19").
        snippet_lines = snippet.split("\n")
        # Extract line numbers from the snippet (format: "   N>>| content" or "   N  | content")
        line_nums = set()
        for sl in snippet_lines:
            # Strip leading whitespace, take digits before >> or |
            stripped = sl.strip()
            if stripped:
                num_part = stripped.split("|")[0].rstrip(">")
                try:
                    line_nums.add(int(num_part.strip()))
                except ValueError:
                    pass
        assert 1 not in line_nums, f"line 1 should not be in snippet, got lines: {sorted(line_nums)}"
        assert 20 not in line_nums, f"line 20 should not be in snippet, got lines: {sorted(line_nums)}"


class TestFormatStructuredError:
    def test_no_match_includes_error_type_and_snippet(self):
        content = "def foo():\n    return 1\n\ndef bar():\n    return 2\n"
        old_string = "def foo():\n    return 99"
        new_string = "def foo():\n    return 42"
        result = format_structured_error(
            "Could not find a match for old_string in the file",
            0, old_string, new_string, content,
            file_path="/tmp/test.py",
        )
        assert "Error type: no_match" in result
        assert "File: /tmp/test.py" in result
        # Should include the closest matching region
        assert "return 1" in result
        # Should include a recovery suggestion
        assert "Recovery:" in result

    def test_ambiguous_includes_error_type_and_recovery(self):
        content = "aaa bbb aaa\n"
        old_string = "aaa"
        new_string = "ccc"
        result = format_structured_error(
            "Found 2 matches for old_string",
            0, old_string, new_string, content,
            file_path="/tmp/test.py",
        )
        assert "Error type: ambiguous" in result
        assert "replace_all=True" in result

    def test_identical_includes_error_type_and_recovery(self):
        result = format_structured_error(
            "old_string and new_string are identical",
            0, "foo", "foo", "foo bar\n",
            strategy="identical",
        )
        assert "Error type: identical" in result
        assert "no action needed" in result.lower()

    def test_escape_drift_includes_error_type_and_recovery(self):
        result = format_structured_error(
            "Escape-drift detected: backslash issue",
            0, "old\\'", "new\\'", "content\n",
        )
        assert "Error type: escape_drift" in result
        assert "read_file" in result

    def test_silent_on_permission_error(self):
        result = format_structured_error(
            "Write denied: protected path",
            0, "old", "new", "content\n",
        )
        assert result == ""

    def test_silent_on_unknown_error(self):
        result = format_structured_error(
            "something weird",
            0, "old", "new", "content\n",
        )
        assert result == ""

    def test_silent_on_none_error(self):
        result = format_structured_error(None, 0, "old", "new", "content\n")
        assert result == ""

    def test_no_match_no_snippet_falls_back_to_generic_recovery(self):
        """When no similar content exists, still provide a generic recovery hint."""
        result = format_structured_error(
            "Could not find a match for old_string in the file",
            0, "totally_unique_xyzzy", "replacement",
            "completely different content\n",
        )
        assert "Error type: no_match" in result
        assert "read_file" in result

    def test_file_path_optional(self):
        result = format_structured_error(
            "Could not find a match for old_string in the file",
            0, "foo", "bar", "def foo():\n    pass\n",
        )
        assert "Error type: no_match" in result
        # Should still work without file_path
        assert "File:" not in result


class TestPatchResultStructuredError:
    """Verify that PatchResult carries structured_error through to_dict."""

    def test_to_dict_includes_diagnostic_when_set(self):
        from tools.file_operations import PatchResult
        result = PatchResult(
            error="Could not find a match for old_string",
            structured_error="Error type: no_match — old_string not found in file",
        )
        d = result.to_dict()
        assert "_diagnostic" in d
        assert "no_match" in d["_diagnostic"]

    def test_to_dict_omits_diagnostic_when_none(self):
        from tools.file_operations import PatchResult
        result = PatchResult(error="some error")
        d = result.to_dict()
        assert "_diagnostic" not in d

    def test_to_dict_omits_diagnostic_on_success(self):
        from tools.file_operations import PatchResult
        result = PatchResult(success=True, diff="--- a\n+++ b\n")
        d = result.to_dict()
        assert "_diagnostic" not in d


class TestSelfCorrectionConfigGate:
    """Verify the config gate for self-correction retry threshold."""

    def test_default_value(self, monkeypatch):
        """When config is unavailable, the default of 3 is returned."""
        import tools.file_tools as ft
        # Reset the cache
        monkeypatch.setattr(ft, "_self_correction_retries_cached", None)
        # Force the config load to fail
        monkeypatch.setattr("hermes_cli.config.load_config", lambda: (_ for _ in ()).throw(Exception("no config")))
        monkeypatch.setattr(ft, "_self_correction_retries_cached", None)
        assert ft._get_self_correction_retries() == 3

    def test_clamps_to_max(self, monkeypatch):
        """Values above 5 are rejected in favor of the default."""
        import tools.file_tools as ft
        monkeypatch.setattr(ft, "_self_correction_retries_cached", None)
        monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"patch": {"self_correction_retries": 99}})
        monkeypatch.setattr(ft, "_self_correction_retries_cached", None)
        assert ft._get_self_correction_retries() == 3

    def test_clamps_to_min(self, monkeypatch):
        """Values below 1 are rejected in favor of the default."""
        import tools.file_tools as ft
        monkeypatch.setattr(ft, "_self_correction_retries_cached", None)
        monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"patch": {"self_correction_retries": 0}})
        monkeypatch.setattr(ft, "_self_correction_retries_cached", None)
        assert ft._get_self_correction_retries() == 3

    def test_valid_value_used(self, monkeypatch):
        """A valid value (1-5) is used as-is."""
        import tools.file_tools as ft
        monkeypatch.setattr(ft, "_self_correction_retries_cached", None)
        monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"patch": {"self_correction_retries": 5}})
        monkeypatch.setattr(ft, "_self_correction_retries_cached", None)
        assert ft._get_self_correction_retries() == 5


class TestPatchFailureEscalationWithDiagnostic:
    """Verify that the structured _diagnostic suppresses redundant _hint."""

    @pytest.fixture
    def hermes_home(self, monkeypatch, tmp_path):
        home = tmp_path / "hermes"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        yield home
        try:
            from tools.file_tools import clear_file_ops_cache
            clear_file_ops_cache()
        except Exception:
            pass

    @pytest.fixture
    def fresh_tracker(self):
        from tools.file_tools import _patch_failure_tracker, _patch_failure_lock
        with _patch_failure_lock:
            _patch_failure_tracker.clear()
        yield
        with _patch_failure_lock:
            _patch_failure_tracker.clear()

    def test_first_failure_has_diagnostic_not_generic_hint(
        self, hermes_home, tmp_path, fresh_tracker, monkeypatch
    ):
        """On the first no-match failure, the structured _diagnostic should
        be present and the generic _hint should be suppressed (since the
        diagnostic is strictly more useful)."""
        from tools.file_tools import _handle_patch
        # Reset the config cache so we get the default threshold
        import tools.file_tools as ft
        monkeypatch.setattr(ft, "_self_correction_retries_cached", None)

        target = tmp_path / "f.py"
        target.write_text("def foo():\n    return 1\n")

        result = _handle_patch(
            {
                "mode": "replace",
                "path": str(target),
                "old_string": "class TOTALLY_NONEXISTENT_BANANA_XYZQQQ:\n    pass",
                "new_string": "x",
            },
            task_id="diag_t1",
        )
        d = json.loads(result)
        # Should have an error (no match)
        assert d.get("error"), f"Expected error for non-matching old_string, got: {d}"
        # Should have the structured diagnostic
        assert "_diagnostic" in d, f"Missing _diagnostic in {list(d.keys())}"
        assert "no_match" in d["_diagnostic"]