"""Tests for evolution_autoformat.py (#1540).

Covers: get_changed_files, format_files (success/failure/dry-run), CLI main().
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from evolution_autoformat import format_files, get_changed_files, main  # noqa: E402


class _FakeProc:
    """Mimic subprocess.run result."""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class TestGetChangedFiles:
    def test_filters_to_python_only(self, tmp_path):
        git_out = "scripts/foo.py\nREADME.md\nbar.py\n"
        with patch("evolution_autoformat._run", return_value=(0, git_out, "")):
            with patch("pathlib.Path.exists", return_value=True):
                with patch("pathlib.Path.cwd", return_value=tmp_path):
                    files = get_changed_files("origin/main", cwd=str(tmp_path))
        assert "scripts/foo.py" in files and "bar.py" in files
        assert "README.md" not in files

    def test_returns_empty_on_git_error(self):
        with patch("evolution_autoformat._run", return_value=(1, "", "fatal")):
            assert get_changed_files("bad-ref") == []


class TestFormatFiles:
    def test_empty_file_list(self):
        success, msgs = format_files([])
        assert success is True and "no Python files" in msgs[0]

    def test_success_path(self):
        runner = MagicMock(side_effect=[_FakeProc(0, "Fixed 1"), _FakeProc(0, "ok")])
        success, _ = format_files(["a.py", "b.py"], runner=runner)
        assert success is True and runner.call_count == 2

    def test_format_failure(self):
        runner = MagicMock(
            side_effect=[_FakeProc(0, ""), _FakeProc(1, "", "fmt error")]
        )
        success, msgs = format_files(["a.py"], runner=runner)
        assert success is False and any("FAILED" in m for m in msgs)

    def test_dry_run_already_clean(self):
        runner = MagicMock(side_effect=[_FakeProc(0, ""), _FakeProc(0, "")])
        success, msgs = format_files(["a.py"], dry_run=True, runner=runner)
        assert success is True and any("already formatted" in m for m in msgs)


class TestMain:
    def test_no_changed_files(self, capsys):
        with patch("evolution_autoformat.get_changed_files", return_value=[]):
            assert main([]) == 0
        assert "nothing to format" in capsys.readouterr().out

    def test_explicit_files(self):
        with patch("evolution_autoformat.format_files", return_value=(True, ["ok"])):
            assert main(["--files", "a.py,b.py"]) == 0

    def test_format_failure_exit_code(self):
        with patch("evolution_autoformat.format_files", return_value=(False, ["FAILED"])):
            assert main(["--files", "a.py"]) == 1
