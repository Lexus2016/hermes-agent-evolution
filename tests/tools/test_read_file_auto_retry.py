#!/usr/bin/env python3
"""Tests for read_file auto-retry-with-correction (#2411).

Covers the ``confident_nearby_match`` gate (sole >=90 candidate, regular
file, ambiguous veto) and the read_file_tool wiring: a confident typo is
retried with the corrected path; ambiguous or failing retries fall back
to the existing hint-only error contract.
"""

import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from tools.file_tools import read_file_tool
from tools.path_validation import confident_nearby_match


class _TmpDir(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="test_autoretry_")
        self.config = os.path.join(self._tmp, "config.yaml")
        with open(self.config, "w") as f:
            f.write("key: value\n")

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)


class TestConfidentNearbyMatch(_TmpDir):
    def test_none_for_existing_path(self):
        self.assertIsNone(confident_nearby_match(self.config))

    def test_same_stem_typo_corrected(self):
        self.assertEqual(
            confident_nearby_match(os.path.join(self._tmp, "config.yam")),
            self.config,
        )

    def test_ambiguous_twin_returns_none(self):
        with open(os.path.join(self._tmp, "config.yml"), "w") as f:
            f.write("other\n")
        self.assertIsNone(
            confident_nearby_match(os.path.join(self._tmp, "config.yam"))
        )

    def test_weak_match_returns_none(self):
        self.assertIsNone(
            confident_nearby_match(os.path.join(self._tmp, "zebra.py"))
        )


class TestReadFileAutoRetry(_TmpDir):
    """read_file_tool must retry once on a confident correction (#2411)."""

    @staticmethod
    def _fail(path):
        result = MagicMock()
        result.to_dict.return_value = {"error": f"File not found: {path}"}
        return result

    def test_confident_typo_retried_automatically(self):
        missing = os.path.join(self._tmp, "config.yam")
        ok = MagicMock()
        ok.to_dict.return_value = {"content": "key: value\n", "path": self.config}
        ops = MagicMock()
        ops.read_file.side_effect = [self._fail(missing), ok]
        with (
            patch("tools.file_tools._get_file_ops", return_value=ops),
            patch.dict(os.environ, {"TERMINAL_CWD": self._tmp}),
        ):
            result = json.loads(read_file_tool(missing, task_id="test_retry_ok"))
        self.assertNotIn("error", result)
        self.assertEqual(result["content"], "key: value\n")
        self.assertEqual(result["auto_corrected"]["requested"], missing)
        self.assertEqual(result["auto_corrected"]["read"], self.config)

    def test_ambiguous_twin_keeps_hint_only_error(self):
        with open(os.path.join(self._tmp, "config.yml"), "w") as f:
            f.write("other\n")
        missing = os.path.join(self._tmp, "config.yam")
        ops = MagicMock()
        ops.read_file.return_value = self._fail(missing)
        with (
            patch("tools.file_tools._get_file_ops", return_value=ops),
            patch.dict(os.environ, {"TERMINAL_CWD": self._tmp}),
        ):
            result = json.loads(read_file_tool(missing, task_id="test_retry_amb"))
        err = result.get("error") or ""
        self.assertIn("Did you mean", err)
        self.assertNotIn("auto_corrected", result)

    def test_failed_retry_falls_back_to_hint(self):
        missing = os.path.join(self._tmp, "config.yam")
        ops = MagicMock()
        ops.read_file.side_effect = [
            self._fail(missing),
            self._fail(self.config),  # retry attempt also fails
        ]
        with (
            patch("tools.file_tools._get_file_ops", return_value=ops),
            patch.dict(os.environ, {"TERMINAL_CWD": self._tmp}),
        ):
            result = json.loads(read_file_tool(missing, task_id="test_retry_fb"))
        err = result.get("error") or ""
        self.assertIn("File not found", err)
        self.assertIn("Did you mean", err)
        self.assertNotIn("auto_corrected", result)


if __name__ == "__main__":
    unittest.main()
