"""Unit and integration tests for read_file auto-repair on file-not-found (#2411).

Tests the 6 recovery strategies of _find_auto_repaired_path and the end-to-end
read_file_tool auto-repair behavior.
"""

import json
import os
from pathlib import Path

from tools.file_tools import _find_auto_repaired_path, read_file_tool


class TestFindAutoRepairedPath:
    """Test individual recovery strategies in _find_auto_repaired_path."""

    def test_case_insensitive_same_dir(self, tmp_path):
        target = tmp_path / "README.md"
        target.write_text("# Hello\n")

        requested = tmp_path / "readme.md"
        hit, note = _find_auto_repaired_path(requested, "readme.md")
        assert hit == target
        assert note is not None
        assert "case-corrected filename" in note
        assert "README.md" in note

    def test_case_insensitive_nested_path(self, tmp_path):
        sub = tmp_path / "src" / "tools"
        sub.mkdir(parents=True)
        target = sub / "file_tools.py"
        target.write_text("# tools\n")

        requested = tmp_path / "Src" / "Tools" / "File_Tools.py"
        hit, note = _find_auto_repaired_path(requested, "Src/Tools/File_Tools.py")
        assert hit is not None
        assert hit.samefile(target)
        assert hit.name == "file_tools.py"
        assert note is not None
        assert "case-corrected" in note

    def test_workspace_root_fallback(self, tmp_path, monkeypatch):
        ws_root = tmp_path / "workspace"
        ws_root.mkdir()
        sub_dir = ws_root / "tests"
        sub_dir.mkdir()
        target = ws_root / "config.yaml"
        target.write_text("key: value\n")

        monkeypatch.setenv("TERMINAL_CWD", str(sub_dir))
        monkeypatch.setenv("HERMES_WORKSPACE_ROOT", str(ws_root))

        requested = sub_dir / "config.yaml"
        hit, note = _find_auto_repaired_path(
            requested, "config.yaml", task_id="test_ws"
        )
        assert hit == target
        assert note is not None
        assert "relative to" in note
        assert "instead of working directory" in note

    def test_prefix_stripping_repo_name(self, tmp_path, monkeypatch):
        repo_dir = tmp_path / "my-repo"
        repo_dir.mkdir()
        target = repo_dir / "tools" / "helper.py"
        target.parent.mkdir()
        target.write_text("# helper\n")

        monkeypatch.setenv("TERMINAL_CWD", str(repo_dir))
        monkeypatch.setenv("HERMES_WORKSPACE_ROOT", str(repo_dir))

        requested = repo_dir / "my-repo" / "tools" / "helper.py"
        hit, note = _find_auto_repaired_path(
            requested, "my-repo/tools/helper.py", task_id="test_prefix"
        )
        assert hit == target
        assert note is not None
        assert "Stripped leading directory prefix" in note

    def test_unique_workspace_file_lookup(self, tmp_path, monkeypatch):
        ws = tmp_path / "project"
        ws.mkdir()
        nested = ws / "deeply" / "nested" / "module"
        nested.mkdir(parents=True)
        target = nested / "special_service_logic.py"
        target.write_text("# special\n")

        monkeypatch.setenv("TERMINAL_CWD", str(ws))
        monkeypatch.setenv("HERMES_WORKSPACE_ROOT", str(ws))

        requested = ws / "special_service_logic.py"
        hit, note = _find_auto_repaired_path(
            requested, "special_service_logic.py", task_id="test_unique"
        )
        assert hit == target
        assert note is not None
        assert "Found unique matching file" in note

    def test_generic_filename_not_globbed(self, tmp_path, monkeypatch):
        ws = tmp_path / "project"
        ws.mkdir()
        (ws / "a").mkdir()
        (ws / "b").mkdir()
        (ws / "a" / "__init__.py").write_text("# a\n")
        (ws / "b" / "__init__.py").write_text("# b\n")

        monkeypatch.setenv("TERMINAL_CWD", str(ws))

        requested = ws / "__init__.py"
        hit, note = _find_auto_repaired_path(
            requested, "__init__.py", task_id="test_generic"
        )
        assert hit is None
        assert note is None

    def test_nonexistent_file_returns_none(self, tmp_path):
        requested = tmp_path / "completely_missing_xyz123.py"
        hit, note = _find_auto_repaired_path(requested, "completely_missing_xyz123.py")
        assert hit is None
        assert note is None


class TestReadFileToolAutoRepairE2E:
    """End-to-end tests for read_file_tool auto-repairing."""

    def test_read_file_case_correction_e2e(self, tmp_path, monkeypatch):
        from unittest.mock import patch
        from tools.file_tools import _get_file_ops
        from tools.file_operations import ReadResult

        target = tmp_path / "AGENTS.md"
        target.write_text("# Agents Guidelines\nline 2\nline 3\n")
        monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))

        orig_ops = _get_file_ops("e2e_case")
        real_read = orig_ops.read_file

        def mock_read(path, offset=1, limit=2000):
            if "agents.md" in str(path):
                return ReadResult(error=f"File not found: {path}")
            return real_read(path, offset, limit)

        with patch.object(orig_ops, "read_file", side_effect=mock_read):
            res_json = read_file_tool("agents.md", task_id="e2e_case")
            res = json.loads(res_json)

            assert "Agents Guidelines" in res.get("content", "")
            assert "hint" in res
            assert "case-corrected filename 'AGENTS.md'" in res["hint"]

    def test_read_file_prefix_stripped_e2e(self, tmp_path, monkeypatch):
        repo_dir = tmp_path / "my-app"
        repo_dir.mkdir()
        src = repo_dir / "src"
        src.mkdir()
        target = src / "index.ts"
        target.write_text("export const answer = 42;\n")

        monkeypatch.setenv("TERMINAL_CWD", str(repo_dir))
        monkeypatch.setenv("HERMES_WORKSPACE_ROOT", str(repo_dir))

        res_json = read_file_tool("my-app/src/index.ts", task_id="e2e_prefix")
        res = json.loads(res_json)

        assert "answer = 42" in res.get("content", "")
        assert "hint" in res
        assert "Stripped leading directory prefix" in res["hint"]

    def test_read_file_nonexistent_still_shows_not_found(self, tmp_path, monkeypatch):
        (tmp_path / "hello.py").write_text("print('hello')\n")
        monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))

        res_json = read_file_tool("nonexistent_unknown_file.py", task_id="e2e_nf")
        res = json.loads(res_json)

        assert "error" in res
        assert "File not found" in res["error"]
