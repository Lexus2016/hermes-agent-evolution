"""Tests for read_file anti-spiral fallback (#886) and patch resilience (#889)."""

import os
import subprocess
import tempfile
from pathlib import Path

from tools.file_operations import ShellFileOperations
from tools.fuzzy_match import fuzzy_find_and_replace, format_no_match_hint


def _make_file_ops(tmp):
    """Build a ShellFileOperations that runs shell cmds in *tmp*."""
    ops = ShellFileOperations.__new__(ShellFileOperations)
    ops._escape_shell_arg = lambda s: f"'{s}'"
    ops._expand_path = lambda s: s if s.startswith("/") else str(tmp / s)

    def _exec(cmd):
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, cwd=str(tmp), timeout=5)

        class _R:
            exit_code = r.returncode
            stdout = r.stdout
            stderr = r.stderr

        return _R()

    ops._exec = _exec
    return ops


class TestSuggestSimilarFilesAntiSpiral:
    def setup_method(self):
        self._d = tempfile.TemporaryDirectory()
        self.tmp = Path(self._d.name)
        (self.tmp / "analysis").mkdir()
        (self.tmp / "analysis" / "2026-07-10.json").write_text("{}")
        (self.tmp / "analysis" / "2026-07-09.json").write_text("{}")
        (self.tmp / "config.yaml").write_text("k: v")
        self.ops = _make_file_ops(self.tmp)

    def teardown_method(self):
        self._d.cleanup()

    def test_existing_dir_no_similar_lists_available(self):
        r = self.ops._suggest_similar_files(str(self.tmp / "analysis" / "nope.json"))
        assert "File not found" in r.error and ("Available" in r.error or "2026-07-10" in r.error)

    def test_nonexistent_dir_walks_to_ancestor(self):
        r = self.ops._suggest_similar_files(str(self.tmp / "bad" / "sub" / "f.json"))
        assert "File not found" in r.error and ("does not exist" in r.error or "ancestor" in r.error)

    def test_similar_files_still_returned(self):
        r = self.ops._suggest_similar_files(str(self.tmp / "analysis" / "2026-07-11.json"))
        assert any("2026-07-10" in f for f in r.similar_files)

class TestIdenticalStrings:
    def test_sentinel_strategy(self):
        _, c, st, err = fuzzy_find_and_replace("hello", "hello", "hello")
        assert c == 0 and st == "identical" and err is not None and "identical" in err

    def test_patch_replace_clean_message(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write("def foo():\n    pass\n")
            fp = f.name
        try:
            ops = _make_file_ops(Path(fp).parent)
            ops._expand_path = lambda s: s
            ops._is_write_denied = lambda s: False
            ops._strip_bom = lambda s: (s, False)
            ops._detect_line_ending = lambda s: None
            ops.write_file = lambda p, c: type("W", (), {"error": None})()
            r = ops.patch_replace(fp, "def foo():", "def foo():")
            assert "identical" in r.error.lower() and "no changes" in r.error.lower()
        finally:
            os.unlink(fp)

class TestAmbiguousLineNumbers:
    def test_includes_line_numbers(self):
        _, c, _, err = fuzzy_find_and_replace("x=1\nx=1\nx=1\n", "x=1", "x=2")
        assert c == 0 and err is not None and "3 matches" in err and "line" in err.lower()

    def test_line_numbers_correct(self):
        _, c, _, err = fuzzy_find_and_replace("a\nfoo\nb\nfoo\nc\nfoo\n", "foo", "bar")
        assert c == 0 and err is not None and "3 matches" in err
        for n in ("2", "4", "6"):
            assert n in err

    def test_replace_all_still_works(self):
        new, c, _, err = fuzzy_find_and_replace("x=1\nx=1\n", "x=1", "x=2", replace_all=True)
        assert err is None and c == 2


class TestHintGating:
    def test_identical_no_hint(self):
        assert format_no_match_hint("old_string and new_string are identical", 0, "a", "a") == ""

    def test_ambiguous_no_hint(self):
        assert format_no_match_hint("Found 3 matches.", 0, "a", "a\na\na") == ""

    def test_genuine_no_match_gives_hint(self):
        ct = "def foo():\n    return 42\n\ndef bar():\n    pass\n"
        assert "Did you mean" in format_no_match_hint("Could not find a match", 0, "def foo():\n    return 43", ct)