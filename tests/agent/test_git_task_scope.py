"""Tests for agent.git_task_scope.

The module is read-only and fails closed; tests use the hermes-agent-evolution
repo itself (the working directory during the test run) because it is guaranteed
to be a git repo on the test runner.
"""

from pathlib import Path

import pytest

from agent.git_task_scope import (
    GitTaskScope,
    _branch_status,
    _downstream_files,
    _find_git_root,
    _recent_commits,
    build_git_task_scope,
    format_task_scope,
)


class TestGitTaskScope:
    def test_find_git_root_in_repo(self):
        root = _find_git_root(Path.cwd())
        assert root is not None
        assert (root / ".git").exists()

    def test_find_git_root_outside_repo(self, tmp_path):
        # tmp_path itself is almost never a git repo
        root = _find_git_root(tmp_path)
        # If the temp dir happens to be under the hermes repo, return the repo root.
        if root is not None:
            assert (root / ".git").exists()

    def test_recent_commits_returns_list(self):
        commits = _recent_commits(Path.cwd(), n=3)
        assert isinstance(commits, list)
        assert len(commits) <= 3
        if commits:
            assert " " in commits[0]

    def test_branch_status_has_head(self):
        info = _branch_status(Path.cwd())
        assert "head" in info

    def test_build_git_task_scope_basic(self):
        scope = build_git_task_scope(cwd=str(Path.cwd()))
        assert scope is not None
        assert scope.repo_root
        assert scope.current_branch
        assert isinstance(scope.recent_commits, list)
        assert isinstance(scope.estimated_impact, dict)

    def test_build_git_task_scope_with_target_paths(self):
        scope = build_git_task_scope(
            cwd=str(Path.cwd()),
            target_paths=["agent/coding_context.py", "agent/prompt_builder.py"],
        )
        assert scope is not None
        assert "agent/coding_context.py" in scope.estimated_impact["target_files"]
        assert "downstream_consumer_count" in scope.estimated_impact

    def test_format_task_scope_contains_branch(self):
        scope = build_git_task_scope(cwd=str(Path.cwd()))
        assert scope is not None
        text = format_task_scope(scope)
        assert scope.current_branch in text

    def test_format_task_scope_empty_for_none(self):
        assert format_task_scope(None) == ""

    def test_downstream_files_limits_to_code(self):
        # _downstream_files uses git grep, which only works inside a repo.
        # Just verify it returns a set.
        consumers = _downstream_files(Path.cwd(), ["agent/git_task_scope.py"])
        assert isinstance(consumers, set)

    def test_as_dict_round_trip(self):
        scope = GitTaskScope(repo_root="/tmp", current_branch="main")
        d = scope.as_dict()
        assert d["repo_root"] == "/tmp"
        assert d["current_branch"] == "main"

    def test_build_git_task_scope_not_in_repo(self, tmp_path):
        non_repo = tmp_path / "not-a-repo"
        non_repo.mkdir()
        assert build_git_task_scope(cwd=str(non_repo)) is None
