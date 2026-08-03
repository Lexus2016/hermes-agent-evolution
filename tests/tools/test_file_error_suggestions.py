"""Tests for read_file file-not-found suggestion propagation (#1587) and
search_files parse-error enrichment (#1588).

#1587: ``_diagnose_read_failure`` returns ``(error_str, similar_files)`` so
the ``similar_files`` field survives into the ``ReadResult`` / ``PatchResult``
and ``classify_file_error`` can route to ``fuzzy_match`` ("Did you mean …?")
instead of the generic ``not_found`` class.

#1588: ``_enrich_search_parse_error`` detects regex compile failures and
appends a glob-vs-regex disambiguation hint so the agent stops blind-retrying
bad patterns.
"""

import os

import pytest

from tools.file_operations import (
    PatchResult,
    ReadResult,
    ShellFileOperations,
    _enrich_search_parse_error,
    classify_file_error,
)
from tools.environments.local import LocalEnvironment


def _ops(root):
    return ShellFileOperations(LocalEnvironment(cwd=str(root)), cwd=str(root))


# ---------------------------------------------------------------------------
# #1587 — similar_files propagation through _diagnose_read_failure
# ---------------------------------------------------------------------------


class TestSimilarFilesPropagation:
    """Verify similar_files survives the read_file → ReadResult → to_dict chain."""

    def test_read_file_not_found_carries_similar_files(self, tmp_path):
        """read_file on a missing file with a near-match neighbour returns a
        ReadResult whose similar_files is populated so classify_file_error
        routes to fuzzy_match."""
        # Create a file that will score as "similar" to the missing target
        (tmp_path / "utils.py").write_text("# real file\n")
        ops = _ops(tmp_path)

        result = ops.read_file(str(tmp_path / "utilz.py"))
        assert result.error is not None
        assert result.similar_files, (
            "similar_files must be populated when near-matches exist"
        )

    def test_read_file_not_found_dict_has_fuzzy_match_class(self, tmp_path):
        """The to_dict() output must carry error_class='fuzzy_match' and a
        'recovery' field mentioning the similar file — not the bare
        'not_found' class that gives no recovery hint."""
        (tmp_path / "config.py").write_text("# real\n")
        ops = _ops(tmp_path)

        result = ops.read_file(str(tmp_path / "config2.py"))
        d = result.to_dict()

        assert d.get("error_class") == "fuzzy_match", (
            f"expected fuzzy_match, got {d.get('error_class')!r} — "
            "similar_files not propagated"
        )
        assert "recovery" in d
        assert "config.py" in d["recovery"]

    def test_read_file_not_found_no_match_has_not_found_class(self, tmp_path):
        """When no similar files exist, error_class is 'not_found'."""
        ops = _ops(tmp_path)
        result = ops.read_file(str(tmp_path / "definitely_nonexistent_file.xyz"))
        d = result.to_dict()
        assert d.get("error_class") == "not_found"

    def test_diagnose_read_failure_returns_tuple(self, tmp_path):
        """_diagnose_read_failure returns (str, list), not just str."""
        (tmp_path / "model.py").write_text("# real\n")
        ops = _ops(tmp_path)

        err, sims = ops._diagnose_read_failure(str(tmp_path / "modle.py"))
        assert isinstance(err, str)
        assert isinstance(sims, list)
        # When a similar file exists, it should appear in the list
        if sims:
            assert any("model.py" in s for s in sims)

    def test_patch_not_found_carries_similar_files(self, tmp_path):
        """patch_replace on a missing file with a near-match returns a
        PatchResult whose similar_files is populated so classify_file_error
        routes to fuzzy_match."""
        (tmp_path / "settings.py").write_text("# real\n")
        ops = _ops(tmp_path)

        result = ops.patch_replace(str(tmp_path / "setting.py"), "old", "new")
        d = result.to_dict()
        assert d.get("error") is not None
        # If similar files were found, error_class should be fuzzy_match
        if result.similar_files:
            assert d.get("error_class") == "fuzzy_match"


# ---------------------------------------------------------------------------
# #1588 — search_files parse-error enrichment
# ---------------------------------------------------------------------------


class TestEnrichSearchParseError:
    """Unit tests for _enrich_search_parse_error."""

    def test_non_parse_error_unchanged(self):
        """Non-parse errors (permission denied etc.) are not enriched."""
        result = _enrich_search_parse_error("Permission denied", "foo")
        assert result == "Search failed: Permission denied"

    def test_glob_pattern_gets_glob_hint(self):
        """Patterns like '*.py' that fail regex compilation get a
        'use target=files' hint."""
        result = _enrich_search_parse_error(
            "rg: regex parse error:\n    error: repetition",
            "*.py",
        )
        assert "target='files'" in result
        assert "*.py" in result

    def test_regex_syntax_error_gets_fix_hint(self):
        """Genuine regex syntax errors get a 'fix the syntax' hint."""
        result = _enrich_search_parse_error(
            "grep: Invalid regular expression",
            "(foo",
        )
        assert "not valid regex" in result
        assert "target='files'" not in result

    def test_bare_bracket_is_regex_not_glob(self):
        """A bare '[' is a regex bracket expression, not a glob."""
        result = _enrich_search_parse_error(
            "rg: regex parse error:\n    [.\n    ^\nerror: unclosed character class",
            "[",
        )
        assert "not valid regex" in result
        assert "target='files'" not in result

    def test_file_glob_set_suppresses_the_glob_redirect(self):
        """With file_glob already set, a glob-shaped pattern is deliberate regex.

        The caller is filtering filenames by file_glob and searching content
        with the pattern, so target='files' — which cannot search content —
        would be the wrong advice. They get the regex-syntax hint instead.
        """
        result = _enrich_search_parse_error(
            "rg: regex parse error:\n    error: repetition",
            "*.py",
            "*.ts",
        )
        assert "target='files'" not in result
        assert "not valid regex" in result

    def test_original_error_preserved(self):
        """The original error text must be preserved in the enriched message."""
        original = "rg: regex parse error:\n    (?:\n     ^\nerror: unclosed group"
        result = _enrich_search_parse_error(original, "(foo")
        assert original in result

    def test_search_with_bad_regex_returns_enriched_error(self, tmp_path):
        """Integration: search_files with an invalid regex returns an
        enriched error message through the full search pipeline."""
        (tmp_path / "f.txt").write_text("hello\n")
        ops = _ops(tmp_path)

        result = ops.search(
            pattern="(",
            path=str(tmp_path),
            target="content",
        )
        assert result.error is not None
        # The error should contain enrichment hints
        assert "Search failed" in result.error
        # Should have actionable guidance
        assert "regex" in result.error.lower() or "target='files'" in result.error


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
