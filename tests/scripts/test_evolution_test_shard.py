"""Tests for scripts/evolution_test_shard.py — file-to-test-shard mapping."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import evolution_test_shard as shard  # noqa: E402


@pytest.fixture
def repo(tmp_path):
    """Create a minimal repo-shaped tree."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / "agent").mkdir()
    (root / "tests" / "agent").mkdir(parents=True)
    (root / "scripts").mkdir()
    (root / "tests" / "scripts").mkdir(parents=True)
    return root


def _touch(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# test", encoding="utf-8")


class TestStemAndPaths:
    def test_stem(self):
        assert shard._stem("agent/retry_utils.py") == "retry_utils"

    def test_is_source_file_true(self):
        assert shard._is_source_file("agent/retry_utils.py") is True

    def test_is_source_file_skips_tests(self):
        assert shard._is_source_file("tests/agent/test_retry_utils.py") is False

    def test_is_source_file_skips_non_py(self):
        assert shard._is_source_file("README.md") is False

    def test_mirrored_test(self):
        assert (
            shard._mirrored_test("agent/retry_utils.py")
            == "tests/agent/test_retry_utils.py"
        )


class TestCollectCandidates:
    def test_mirrored_test_found(self, repo):
        _touch(repo / "tests" / "agent" / "test_retry_utils.py")
        files, _ = shard._collect_candidates("agent/retry_utils.py", repo)
        assert "tests/agent/test_retry_utils.py" in files

    def test_same_dir_test_found(self, repo):
        _touch(repo / "agent" / "test_retry_utils_extra.py")
        files, _ = shard._collect_candidates("agent/retry_utils.py", repo)
        assert "agent/test_retry_utils_extra.py" in files

    def test_tests_dir_pattern_found(self, repo):
        _touch(repo / "tests" / "agent" / "test_retry_utils_foo.py")
        files, _ = shard._collect_candidates("agent/retry_utils.py", repo)
        assert "tests/agent/test_retry_utils_foo.py" in files

    def test_fallback_dirs_returned(self, repo):
        _, falls = shard._collect_candidates("agent/retry_utils.py", repo)
        assert "tests/agent" in falls


class TestBuildShard:
    def test_concrete_test_shard(self, repo):
        _touch(repo / "tests" / "agent" / "test_retry_utils.py")
        result = shard.build_shard(["agent/retry_utils.py"], repo)
        assert result["command"] == [
            "python",
            "-m",
            "pytest",
            "tests/agent/test_retry_utils.py",
            "-q",
        ]
        assert result["test_files"] == ["tests/agent/test_retry_utils.py"]
        assert result["fallback_dirs"] == []
        assert "mirrored" in result["heuristic"]

    def test_directory_fallback(self, repo):
        result = shard.build_shard(["agent/retry_utils.py"], repo)
        assert result["command"] == ["python", "-m", "pytest", "tests/agent", "-q"]
        assert result["test_files"] == []
        assert result["fallback_dirs"] == ["tests/agent"]

    def test_test_only_change(self, repo):
        _touch(repo / "tests" / "agent" / "test_retry_utils.py")
        result = shard.build_shard(["tests/agent/test_retry_utils.py"], repo)
        assert result["test_files"] == ["tests/agent/test_retry_utils.py"]
        assert "test-only" in result["heuristic"]

    def test_multiple_sources_deduplicated(self, repo):
        _touch(repo / "tests" / "agent" / "test_retry_utils.py")
        _touch(repo / "tests" / "scripts" / "test_evolution_foo.py")
        result = shard.build_shard(
            ["agent/retry_utils.py", "scripts/evolution_foo.py"], repo
        )
        assert "tests/agent/test_retry_utils.py" in result["test_files"]
        assert "tests/scripts/test_evolution_foo.py" in result["test_files"]

    def test_no_tests_empty_command(self, tmp_path):
        result = shard.build_shard(["agent/retry_utils.py"], tmp_path)
        assert result["command"] == []


class TestCLI:
    def test_cli_with_positional_args(self, tmp_path, monkeypatch, capsys):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "tests" / "agent").mkdir(parents=True)
        _touch(repo / "tests" / "agent" / "test_retry_utils.py")
        monkeypatch.setenv("EVOLUTION_REPO_DIR", str(repo))
        rc = shard.main(["evolution_test_shard.py", "agent/retry_utils.py"])
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["test_files"] == ["tests/agent/test_retry_utils.py"]

    def test_cli_with_git_diff(self, tmp_path, monkeypatch, capsys):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "tests" / "agent").mkdir(parents=True)
        _touch(repo / "tests" / "agent" / "test_retry_utils.py")
        # Make it a git repo and stage a source file so git diff sees it as new.
        import subprocess

        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "x@x.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "x"], cwd=repo, check=True)
        _touch(repo / "agent" / "retry_utils.py")
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "init", "-q"], cwd=repo, check=True)
        # Stage a change so git diff sees it.
        (repo / "agent" / "retry_utils.py").write_text("# modified", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        monkeypatch.setenv("EVOLUTION_REPO_DIR", str(repo))
        rc = shard.main(["evolution_test_shard.py"])
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["test_files"] == ["tests/agent/test_retry_utils.py"]
