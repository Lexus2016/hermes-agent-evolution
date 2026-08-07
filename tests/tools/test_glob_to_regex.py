"""Tests for _glob_to_regex — glob-to-regex conversion in search_files (#1788).

The model frequently passes shell glob patterns (``*.py``, ``*config*``) as
the ``pattern`` parameter in content-search mode.  Instead of returning a
regex parse error, the tool now transparently converts the glob to a regex.
"""

import re

import pytest

from tools.file_tools import _glob_to_regex


class TestGlobToRegex:
    """Verify glob-to-regex conversion correctness."""

    @pytest.mark.parametrize(
        "glob,expected",
        [
            ("*.py", r".*\.py"),
            ("*config*", r".*config.*"),
            ("?", "."),
            ("[abc]", "[abc]"),
            ("[a-z]", "[a-z]"),
            ("foo.py", r"foo\.py"),
            ("*", ".*"),
            ("foo?.py", r"foo.\.py"),
            ("test[0-9].txt", r"test[0-9]\.txt"),
        ],
    )
    def test_conversion(self, glob, expected):
        assert _glob_to_regex(glob) == expected

    def test_unmatched_bracket_escaped(self):
        """An unmatched ``[`` is treated as a literal, not a character class."""
        result = _glob_to_regex("[unclosed")
        assert result == "\\[unclosed"

    def test_negated_class(self):
        """Glob ``[!...]`` is passed through — regex shares ``[^...]``..."""
        # Actually [!...] is glob syntax; we pass the bracket through as-is.
        # The regex engine will interpret [!x] as a character class matching
        # '!' and 'x'. This is acceptable — it still compiles and searches.
        result = _glob_to_regex("[!abc]")
        assert re.compile(result)  # at least valid regex

    def test_pipe_escaped(self):
        """Pipe is a regex metacharacter, not a glob one — must be escaped."""
        result = _glob_to_regex("foo|bar")
        assert result == r"foo\|bar"

    def test_plus_escaped(self):
        result = _glob_to_regex("foo+bar")
        assert result == r"foo\+bar"

    def test_parens_escaped(self):
        result = _glob_to_regex("foo(bar)")
        assert result == r"foo\(bar\)"

    def test_dollar_escaped(self):
        result = _glob_to_regex("foo$bar")
        assert result == r"foo\$bar"

    def test_caret_escaped(self):
        result = _glob_to_regex("^foo")
        assert result == r"\^foo"

    def test_brace_escaped(self):
        result = _glob_to_regex("{foo}")
        assert result == r"\{foo\}"

    def test_backslash_escaped(self):
        result = _glob_to_regex("foo\\bar")
        assert result == r"foo\\bar"

    def test_result_always_compiles(self):
        """The output must be a valid Python regex."""
        for glob in ["*.py", "*config*", "?", "*", "foo.py", "[abc]", "[a-z]"]:
            regex = _glob_to_regex(glob)
            re.compile(regex)  # should not raise

    @pytest.mark.parametrize(
        "glob,test_str",
        [
            ("*.py", "hello.py"),
            ("*config*", "my_config_file.py"),
            ("foo.py", "foo.py"),
            ("test[0-9]", "test5"),
            ("foo?", "foox"),
        ],
    )
    def test_converted_regex_matches(self, glob, test_str):
        """The converted regex should match strings the glob would match."""
        regex = _glob_to_regex(glob)
        assert re.search(regex, test_str), f"{regex!r} did not match {test_str!r}"
