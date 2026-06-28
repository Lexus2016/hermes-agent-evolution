"""Tests for scripts/evolution_pre_pr_test_runner.py — pre-PR local test gate (#580).

The gate maps changed source files to targeted test shards and runs them BEFORE
`gh pr create`.  The CRITICAL invariant under test is MAPPING CORRECTNESS:
files must map to the right test paths (exact, module, directory, fallback)
and missing tests must be silently dropped (never invented).

Pure + offline: the existing-paths set + subprocess runner are INJECTED, so
these tests run with no real filesystem or pytest.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from evolution_pre_pr_test_runner import (  # noqa: E402
    DEFAULT_FALLBACK_KWARGS,
    DEFAULT_FALLBACK_TIMEOUT,
    DEFAULT_TIMEOUT,
    SRC_TO_TEST_PREFIX,
    GateReport,
    TestShard,
    TestResult,
    _basename_to_test_basename,
    _resolve_test_path,
    get_fallback_shard,
    map_changed_files_to_shards,
    run_gate,
    run_shard,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _set_of(paths: list[str]) -> set[str]:
    return set(paths)


# ── Test: basename mapping ────────────────────────────────────────────────────

class TestBasenameToTestBasename:
    def test_plain_py_prefixes_test(self):
        assert _basename_to_test_basename("foo.py") == "test_foo.py"

    def test_already_test_prefix_is_kept(self):
        assert _basename_to_test_basename("test_foo.py") == "test_foo.py"

    def test_no_py_extension_adds_py(self):
        assert _basename_to_test_basename("foo") == "test_foo.py"


# ── Test: resolve_test_path ───────────────────────────────────────────────────

class TestResolveTestPath:
    def test_exact_agent_match(self):
        existing = _set_of(["tests/agent/test_foo.py"])
        result = _resolve_test_path(
            "agent/foo.py", _repo_root(), existing_paths=existing
        )
        assert result == "tests/agent/test_foo.py"

    def test_exact_scripts_match(self):
        existing = _set_of(["tests/scripts/test_evolution_triage.py"])
        result = _resolve_test_path(
            "scripts/evolution_triage.py", _repo_root(), existing_paths=existing
        )
        assert result == "tests/scripts/test_evolution_triage.py"

    def test_exact_hermes_cli_match(self):
        existing = _set_of(["tests/hermes_cli/test_config.py"])
        result = _resolve_test_path(
            "hermes_cli/config.py", _repo_root(), existing_paths=existing
        )
        assert result == "tests/hermes_cli/test_config.py"

    def test_exact_tools_match(self):
        existing = _set_of(["tests/tools/test_browser_tool.py"])
        result = _resolve_test_path(
            "tools/browser_tool.py", _repo_root(), existing_paths=existing
        )
        assert result == "tests/tools/test_browser_tool.py"

    def test_module_match_tool_dispatch_helpers(self):
        # Source: agent/tool_dispatch_helpers.py → tests/agent/test_tool_dispatch_helpers.py
        existing = _set_of(["tests/agent/test_tool_dispatch_helpers.py"])
        result = _resolve_test_path(
            "agent/tool_dispatch_helpers.py", _repo_root(), existing_paths=existing
        )
        assert result == "tests/agent/test_tool_dispatch_helpers.py"

    def test_directory_fallback_when_exact_test_missing(self):
        # Exact test file doesn't exist, but the directory does
        existing = _set_of(["tests/hermes_cli/test_config.py"])
        result = _resolve_test_path(
            "hermes_cli/config.py", _repo_root(), existing_paths=existing
        )
        # Exact match test_* exists → it's the file match
        assert result == "tests/hermes_cli/test_config.py"

    def test_directory_fallback_when_no_test_file_exists(self):
        # No test_config.py exists, but tests/hermes_cli/ does
        existing = _set_of(["tests/hermes_cli/test_other.py"])
        result = _resolve_test_path(
            "hermes_cli/config.py", _repo_root(), existing_paths=existing
        )
        # Falls back to directory
        assert result == "tests/hermes_cli"

    def test_parent_directory_fallback_when_subdir_missing(self):
        # scripts/subdir/foo.py → tests/scripts/subdir/ doesn't exist,
        # fall back to tests/scripts/
        existing = _set_of(["tests/scripts/test_other.py"])
        result = _resolve_test_path(
            "scripts/subdir/foo.py", _repo_root(), existing_paths=existing
        )
        assert result == "tests/scripts"

    def test_unknown_prefix_returns_none(self):
        existing = _set_of(["tests/agent/test_foo.py"])
        result = _resolve_test_path(
            "some_unknown_dir/foo.py", _repo_root(), existing_paths=existing
        )
        assert result is None

    def test_top_level_file_returns_none(self):
        existing = _set_of(["tests/scripts/test_blah.py"])
        result = _resolve_test_path(
            "__init__.py", _repo_root(), existing_paths=existing
        )
        assert result is None


# ── Test: map_changed_files_to_shards ─────────────────────────────────────────

class TestMapChangedFilesToShards:
    def test_empty_files_returns_empty(self):
        shards = map_changed_files_to_shards([], _repo_root())
        assert shards == []

    def test_single_agent_file(self):
        existing = _set_of(["tests/agent/test_foo.py"])
        shards = map_changed_files_to_shards(
            ["agent/foo.py"], _repo_root(), existing_paths=existing
        )
        assert len(shards) == 1
        assert shards[0].pytest_args == [
            "tests/agent/test_foo.py", "-x", "-q", f"--timeout={DEFAULT_TIMEOUT}",
        ]

    def test_deduplicates_same_test_file(self):
        # Two source files that both map to the same test file
        existing = _set_of(["tests/tools/test_browser_tool.py"])
        shards = map_changed_files_to_shards(
            ["tools/browser_tool.py", "tools/browser_tool.py"],
            _repo_root(), existing_paths=existing,
        )
        assert len(shards) == 1

    def test_multiple_distinct_shards(self):
        existing = _set_of([
            "tests/agent/test_foo.py",
            "tests/hermes_cli/test_bar.py",
        ])
        shards = map_changed_files_to_shards(
            ["agent/foo.py", "hermes_cli/bar.py"],
            _repo_root(), existing_paths=existing,
        )
        assert len(shards) == 2

    def test_skips_unmappable_files(self):
        existing = _set_of(["tests/agent/test_foo.py"])
        shards = map_changed_files_to_shards(
            ["agent/foo.py", "mystery/unknown.py"],
            _repo_root(), existing_paths=existing,
        )
        assert len(shards) == 1
        assert shards[0].description.startswith("exact:")


# ── Test: fallback shard ──────────────────────────────────────────────────────

class TestFallbackShard:
    def test_fallback_has_correct_args(self):
        s = get_fallback_shard()
        assert "tests/" in s.pytest_args
        assert "-x" in s.pytest_args
        assert "-q" in s.pytest_args
        assert f"--timeout={DEFAULT_FALLBACK_TIMEOUT}" in s.pytest_args
        assert "-k" in s.pytest_args
        assert DEFAULT_FALLBACK_KWARGS in s.pytest_args

    def test_fallback_description(self):
        s = get_fallback_shard()
        assert "fallback" in s.description.lower()


# ── Test: run_gate integration (with injected runner) ─────────────────────────

class FakeRunner:
    """Injectable subprocess runner that returns canned results."""

    def __init__(self, returncode: int = 0, stdout: str = "ok", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.calls: list = []

    def __call__(self, cmd, env):
        self.calls.append((cmd, env))
        return self.returncode, self.stdout, self.stderr


class TestRunGate:
    def test_empty_changed_files_passes(self):
        report = run_gate([], _repo_root())
        assert report.passed is True
        assert report.note == "no changed files — nothing to test"

    def test_all_shards_pass(self, tmp_path):
        existing = _set_of(["tests/agent/test_foo.py"])
        fake_runner = FakeRunner(returncode=0)
        log = tmp_path / "test.log"

        report = run_gate(
            ["agent/foo.py"],
            _repo_root(),
            existing_paths=existing,
            runner=fake_runner,
            log_path=log,
        )

        assert report.passed is True
        assert len(report.results) == 1
        assert report.results[0].returncode == 0
        assert log.exists()

    def test_any_shard_fails_fails_gate(self, tmp_path):
        existing = _set_of([
            "tests/agent/test_foo.py",
            "tests/hermes_cli/test_bar.py",
        ])
        fake_runner = FakeRunner(returncode=1, stdout="FAILED", stderr="error!")
        log = tmp_path / "test.log"

        report = run_gate(
            ["agent/foo.py", "hermes_cli/bar.py"],
            _repo_root(),
            existing_paths=existing,
            runner=fake_runner,
            log_path=log,
        )

        assert report.passed is False
        assert "failed" in report.note.lower()

    def test_fallback_when_no_shards_found(self, tmp_path):
        fake_runner = FakeRunner(returncode=0)
        log = tmp_path / "test.log"

        report = run_gate(
            ["mystery/unknown.py"],
            _repo_root(),
            existing_paths=set(),
            runner=fake_runner,
            log_path=log,
        )

        # Should fall back to the full-suite fallback shard
        assert len(report.shards) == 1
        assert "fallback" in report.shards[0].description.lower()
        assert report.passed is True


# ── Test: run_shard (with injected runner) ────────────────────────────────────

class TestRunShard:
    def test_passing_shard(self):
        shard = TestShard(
            pytest_args=["tests/agent/test_foo.py", "-x", "-q", "--timeout=60"],
            description="test",
        )
        runner = FakeRunner(returncode=0, stdout="2 passed")
        result = run_shard(shard, _repo_root(), runner=runner)
        assert result.returncode == 0
        assert result.stdout == "2 passed"

    def test_failing_shard(self):
        shard = TestShard(
            pytest_args=["tests/agent/test_foo.py"],
            description="test",
        )
        runner = FakeRunner(returncode=1, stdout="1 failed", stderr="traceback")
        result = run_shard(shard, _repo_root(), runner=runner)
        assert result.returncode == 1
        assert "traceback" in result.stderr


# ── Test: data objects ───────────────────────────────────────────────────────

class TestDataObjects:
    def test_testshard_as_cmd(self):
        shard = TestShard(
            pytest_args=["tests/agent/test_foo.py", "-x", "-q"],
            description="test",
        )
        cmd = shard.as_cmd()
        assert cmd[0] == "pytest"
        assert "tests/agent/test_foo.py" in cmd

    def test_gatereport_shape(self):
        report = GateReport(changed_files=["a.py", "b.py"])
        assert len(report.changed_files) == 2
        assert report.shards == []
        assert report.results == []
        assert report.passed is False
        assert report.note == ""


# ── Test: SRC_TO_TEST_PREFIX shape ───────────────────────────────────────────

class TestSrcToTestPrefix:
    def test_all_prefixes_end_with_slash(self):
        for src, tst in SRC_TO_TEST_PREFIX:
            assert src.endswith("/"), f"source prefix {src} must end with /"
            assert tst.endswith("/"), f"test prefix {tst} must end with /"

    def test_expected_prefixes_present(self):
        prefixes = dict(SRC_TO_TEST_PREFIX)
        assert "agent/" in prefixes
        assert "hermes_cli/" in prefixes
        assert "tools/" in prefixes
        assert "scripts/" in prefixes
        assert "tui_gateway/" in prefixes
        assert "cron/" in prefixes
