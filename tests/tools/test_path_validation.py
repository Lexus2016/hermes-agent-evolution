#!/usr/bin/env python3
"""Tests for the shared pure-Python path-validation module (#2293).

Covers ``suggest_nearby_paths`` (existence check + similarity scoring +
nearest-ancestor fallback) and ``format_nearby_hint`` (the #1587 inline
"did you mean?" contract). Also verifies read_file_tool falls back to the
shared module when the shell-based suggestion finds nothing.

Run with:  python -m pytest tests/tools/test_path_validation.py -v
"""

import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from tools.path_validation import format_nearby_hint, suggest_nearby_paths
from tools.file_tools import read_file_tool


class _TmpDir(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="test_pathval_")
        for name in ("config.yaml", "config.yml", "settings.py", "README.md"):
            with open(os.path.join(self._tmp, name), "w") as f:
                f.write("x\n")

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)


class TestSuggestNearbyPaths(_TmpDir):
    def test_existing_path_returns_empty(self):
        self.assertEqual(
            suggest_nearby_paths(os.path.join(self._tmp, "config.yaml")), []
        )

    def test_similar_basename_suggested(self):
        nearby = suggest_nearby_paths(os.path.join(self._tmp, "config.yam"))
        self.assertTrue(nearby)
        self.assertTrue(any("config.yaml" in p for p in nearby))

    def test_missing_parent_walks_to_ancestor(self):
        deep = os.path.join(self._tmp, "nope", "sub", "config.yaml")
        nearby = suggest_nearby_paths(deep)
        self.assertTrue(any("config.yaml" in p for p in nearby))

    def test_max_results_respected(self):
        nearby = suggest_nearby_paths(
            os.path.join(self._tmp, "config.yam"), max_results=1
        )
        self.assertLessEqual(len(nearby), 1)


class TestFormatNearbyHint(unittest.TestCase):
    def test_empty_nearby_returns_none(self):
        self.assertIsNone(format_nearby_hint("/x/y", []))

    def test_hint_inlines_did_you_mean(self):
        hint = format_nearby_hint("/x/y.py", ["/x/z.py"])
        self.assertIn("File not found: /x/y.py", hint)
        self.assertIn("Did you mean: /x/z.py", hint)


class TestReadFileToolFallback(_TmpDir):
    """read_file_tool must fall back to the shared module when the
    shell-based _suggest_similar_files finds nothing."""

    def test_fallback_surfaces_nearby_hint(self):
        missing = os.path.join(self._tmp, "config.yam")
        fake_ops = MagicMock()
        fake_ops.read_file.return_value = MagicMock(
            to_dict=lambda: {"error": f"File not found: {missing}", "similar_files": []}
        )
        with (
            patch("tools.file_tools._get_file_ops", return_value=fake_ops),
            patch.dict(os.environ, {"TERMINAL_CWD": self._tmp}),
        ):
            result = read_file_tool(missing, task_id="test_fallback")
        err = json.loads(result).get("error") or ""
        self.assertIn("File not found", err)
        self.assertIn("Did you mean", err)
        self.assertIn("config.yaml", err)


class TestSearchFilesNearbyHint(_TmpDir):
    """#2242 Slice B — search_files must surface a nearby-paths hint when
    the search root path doesn't exist."""

    def test_search_surfaces_nearby_hint(self):
        from tools.file_tools import search_tool

        missing = os.path.join(self._tmp, "sub", "config.yam")  # 'sub' doesn't exist

        class _FakeResult:
            def to_dict(self, **kwargs):
                return {"error": f"Path not found: {missing}", "results": []}

        fake_ops = MagicMock()
        fake_ops.search.return_value = _FakeResult()
        with (
            patch("tools.file_tools._get_file_ops", return_value=fake_ops),
            patch.dict(os.environ, {"TERMINAL_CWD": self._tmp}),
        ):
            result = search_tool(
                pattern="test", path=missing, task_id="test_search_nf"
            )
        err = json.loads(result).get("error") or ""
        self.assertIn("Path not found", err)
        self.assertIn("Did you mean", err)


class TestPatchToolNearbyHint(_TmpDir):
    """#2242 Slice B — patch must surface a nearby-paths hint when the
    target file doesn't exist."""

    def test_patch_replace_surfaces_nearby_hint(self):
        from tools.file_tools import patch_tool

        missing = os.path.join(self._tmp, "config.yam")

        class _FakeResult:
            def to_dict(self, **kwargs):
                return {
                    "error": f"File not found: {missing}",
                    "similar_files": [],
                }

        fake_ops = MagicMock()
        fake_ops.patch_replace.return_value = _FakeResult()
        with (
            patch("tools.file_tools._get_file_ops", return_value=fake_ops),
            patch.dict(os.environ, {"TERMINAL_CWD": self._tmp}),
        ):
            result = patch_tool(
                mode="replace",
                path=missing,
                old_string="x",
                new_string="y",
                task_id="test_patch_nf",
            )
        err = json.loads(result).get("error") or ""
        self.assertIn("File not found", err)
        self.assertIn("Did you mean", err)
        self.assertIn("config.yaml", err)


class TestTerminalNearbyHint(_TmpDir):
    """#2242 Slice B — terminal must surface a nearby-paths hint when a
    command fails with 'No such file or directory'."""

    def test_terminal_surfaces_nearby_hint(self):
        from tools.terminal_tool import terminal_tool

        missing = os.path.join(self._tmp, "config.yam")

        class _FakeResult(dict):
            def __init__(self):
                super().__init__(
                    output=f"cat: {missing}: No such file or directory",
                    returncode=1,
                    error=None,
                )

        class _FakeEnv:
            cwd = None

            def execute(self, command, **kwargs):
                return _FakeResult()

        with (
            patch("tools.terminal_tool.get_active_env", return_value=_FakeEnv()),
            patch.dict(os.environ, {"TERMINAL_CWD": self._tmp}),
        ):
            result = terminal_tool(
                command=f"cat {missing}", task_id="test_term_nf"
            )
        err = json.loads(result).get("error") or ""
        self.assertIn("Did you mean", err)
        self.assertIn("config.yaml", err)


if __name__ == "__main__":
    unittest.main()
