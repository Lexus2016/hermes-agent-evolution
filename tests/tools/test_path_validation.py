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


if __name__ == "__main__":
    unittest.main()
