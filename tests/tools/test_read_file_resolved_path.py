#!/usr/bin/env python3
"""
Test that read_file_tool passes the RESOLVED absolute path to
FileOperations.read_file, not the raw user-supplied path.

This is the root-cause fix for issue #1044: when a relative path was
passed to read_file_tool, _resolve_path_for_task correctly resolved it
to an absolute path (used for binary checks, dedup, etc.), but the raw
relative path was then forwarded to FileOperations.read_file which runs
shell commands (wc, sed, head) from the shell's cwd — which may differ
from the terminal env's tracked cwd.  The file would not be found, and
the agent would spiral through path variations.

Run with:  python -m pytest tests/tools/test_read_file_resolved_path.py -v
"""

import json
import os
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from tools.file_tools import read_file_tool


class _CapturedPathResult:
    """Minimal ReadResult stand-in that captures the path argument."""

    def __init__(self):
        self.content = "line1\nline2\n"
        self.total_lines = 2
        self.file_size = 100
        self.captured_path = None
        self.error = None
        self.hint = None
        self.is_binary = False
        self.is_image = False
        self.truncated = False
        self.similar_files = []

    def to_dict(self):
        d = {
            "content": self.content,
            "total_lines": self.total_lines,
            "file_size": self.file_size,
        }
        return d


class TestReadFileResolvedPath(unittest.TestCase):
    """read_file_tool must pass the resolved absolute path to FileOperations."""

    def setUp(self):
        # Create a temp directory to use as TERMINAL_CWD
        self._tmpdir = tempfile.mkdtemp(
            prefix="test_readfile_resolved_", dir=os.getcwd()
        )
        self._test_file = os.path.join(self._tmpdir, "target.py")
        with open(self._test_file, "w") as f:
            f.write("line1\nline2\nline3\n")

    def tearDown(self):
        import shutil

        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_relative_path_resolved_before_shell_call(self):
        """A relative path must be resolved to absolute before reaching
        FileOperations.read_file, so the shell commands run from the
        correct directory."""
        captured = _CapturedPathResult()

        def _capture_read_file(path, offset=1, limit=500):
            captured.captured_path = path
            return captured

        fake_ops = MagicMock()
        fake_ops.read_file = _capture_read_file

        with (
            patch("tools.file_tools._get_file_ops", return_value=fake_ops),
            patch.dict(os.environ, {"TERMINAL_CWD": self._tmpdir}),
        ):
            # Pass a bare relative filename — no directory component.
            # If the raw path is forwarded, it would be "target.py" and
            # the shell would look in its own cwd (likely the repo root).
            # The fix ensures the resolved absolute path is used.
            result = read_file_tool("target.py", task_id="test_resolved")

        # The path captured by FileOperations.read_file must be the
        # resolved absolute path, not the raw relative "target.py".
        self.assertIsNotNone(captured.captured_path)
        captured_str: str = captured.captured_path or ""
        self.assertTrue(
            os.path.isabs(captured_str),
            f"FileOperations.read_file received a relative path "
            f"'{captured_str}' — it should be absolute.",
        )
        self.assertTrue(
            captured_str.endswith("target.py"),
            f"Resolved path '{captured_str}' should end with 'target.py'",
        )

    def test_absolute_path_passed_through(self):
        """An absolute path should be passed through (resolved) unchanged."""
        captured = _CapturedPathResult()

        def _capture_read_file(path, offset=1, limit=500):
            captured.captured_path = path
            return captured

        fake_ops = MagicMock()
        fake_ops.read_file = _capture_read_file

        with (
            patch("tools.file_tools._get_file_ops", return_value=fake_ops),
            patch.dict(os.environ, {"TERMINAL_CWD": self._tmpdir}),
        ):
            read_file_tool(self._test_file, task_id="test_abs")

        self.assertEqual(
            os.path.realpath(captured.captured_path or ""),
            os.path.realpath(self._test_file),
        )

    def test_relative_path_with_subdir_resolved(self):
        """A relative path with a subdirectory component should also be
        resolved to an absolute path before reaching FileOperations."""
        subdir = os.path.join(self._tmpdir, "subdir")
        os.makedirs(subdir)
        nested_file = os.path.join(subdir, "nested.py")
        with open(nested_file, "w") as f:
            f.write("content\n")

        captured = _CapturedPathResult()

        def _capture_read_file(path, offset=1, limit=500):
            captured.captured_path = path
            return captured

        fake_ops = MagicMock()
        fake_ops.read_file = _capture_read_file

        with (
            patch("tools.file_tools._get_file_ops", return_value=fake_ops),
            patch.dict(os.environ, {"TERMINAL_CWD": self._tmpdir}),
        ):
            read_file_tool("subdir/nested.py", task_id="test_subdir")

        self.assertIsNotNone(captured.captured_path)
        captured_sub: str = captured.captured_path or ""
        self.assertTrue(
            os.path.isabs(captured_sub),
            f"FileOperations.read_file received relative path "
            f"'{captured_sub}' for a subdir-relative input.",
        )


if __name__ == "__main__":
    unittest.main()
