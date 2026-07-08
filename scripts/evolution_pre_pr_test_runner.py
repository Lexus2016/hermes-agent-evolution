#!/usr/bin/env python3
"""Pre-PR local test runner — targeted test shard gate for the implementation pipeline (#580).

WHY THIS EXISTS — shift validation LEFT. Before `gh pr create`, run ONLY the
tests most likely affected by the changed files, so we catch regressions LOCALLY
instead of waiting for CI feedback minutes later. Running the full test suite on
every PR is expensive (~minutes) and noisy; this gate runs a tightly-focused
shard identified by mapping changed source files to their test counterparts.

MAPPING HEURISTIC (four-tier fallback, in order of specificity):

1. **Exact match**: ``agent/foo.py`` → ``pytest tests/agent/test_foo.py -x -q --timeout=60``
2. **Module match**: derived from the basename, e.g.
   ``agent/tool_dispatch_helpers.py`` → ``pytest tests/agent/test_tool_dispatch_helpers.py -x -q --timeout=60``
3. **Directory fallback**: ``hermes_cli/config.py`` → ``pytest tests/hermes_cli/ -x -q --timeout=60``
4. **Last resort**: ``pytest tests/ -x -q --timeout=120 -k "not slow and not docker"``

Only *existing* test files and directories are used — missing tests are silently
dropped (the gate never invents test paths). When multiple source files map to
the same test shard, it is deduplicated. If after dedup there are multiple
shards, they are joined with **OR** logic: any one passing is sufficient
(conservative "at least the relevant shards pass" strategy, rather than an
AND-gate that would block a PR because of a stale unrelated test).

CLI:
    python scripts/evolution_pre_pr_test_runner.py \\
        --changed-files path/to/file1.py,path/to/file2.py

    Or via a file:
        python scripts/evolution_pre_pr_test_runner.py \\
        --changed-files-file /tmp/changed.txt

Returns exit code 0 if all targeted tests pass, 1 if any fail.
Logs the full pytest output to:
    ~/.hermes/evolution/pre-pr-test-results/{timestamp}.log

Standalone design: import-safe pure functions for unit-testability; the CLI
entrypoint orchestrates IO (git diff, subprocess, file logging).
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Set


# ── Constants ──────────────────────────────────────────────────────────────────
DEFAULT_TIMEOUT = 60
DEFAULT_FALLBACK_TIMEOUT = 120
DEFAULT_FALLBACK_KWARGS = "not slow and not docker"

_LOG_DIR_TEMPLATE = "~/.hermes/evolution/pre-pr-test-results"

# Source-root to test-root mapping: each source prefix maps to the
# corresponding test directory prefix, so agent/... → tests/agent/...,
# hermes_cli/... → tests/hermes_cli/..., etc.
SRC_TO_TEST_PREFIX = (
    ("agent/", "tests/agent/"),
    ("hermes_cli/", "tests/hermes_cli/"),
    ("tools/", "tests/tools/"),
    ("tui_gateway/", "tests/tui_gateway/"),
    # Scripts live in scripts/ but their tests are in tests/scripts/ — this
    # is a different mapping: scripts/foo.py → tests/scripts/test_foo.py
    ("scripts/", "tests/scripts/"),
    ("cron/", "tests/cron/"),
)


# ── Data objects ────────────────────────────────────────────────────────────────

@dataclass
class TestShard:
    """A concrete pytest invocation: what to run and with what flags."""

    __test__ = False  # not a pytest test class

    pytest_args: List[str]  # e.g. ["tests/agent/test_foo.py", "-x", "-q", "--timeout=60"]
    description: str  # human label for logging / reporting

    def as_cmd(self) -> List[str]:
        """Full shell command for subprocess (pytest binary + args)."""
        return ["pytest"] + self.pytest_args


@dataclass
class TestResult:
    """Result of running a single test shard."""

    __test__ = False  # not a pytest test class

    shard: TestShard
    returncode: int
    stdout: str
    stderr: str
    elapsed_sec: float


@dataclass
class GateReport:
    """Full outcome of the pre-PR test gate."""

    changed_files: List[str]
    shards: List[TestShard] = field(default_factory=list)
    results: List[TestResult] = field(default_factory=list)
    passed: bool = False
    note: str = ""


# ── File → test mapping (pure, injectable) ─────────────────────────────────────

def _basename_to_test_basename(basename: str) -> str:
    """Map ``foo.py`` → ``test_foo.py``.

    If the file already starts with ``test_`` it is kept as-is (handles the
    edge case of changed files that are themselves test files).
    """
    if basename.startswith("test_") and basename.endswith(".py"):
        return basename
    if basename.endswith(".py"):
        return f"test_{basename}"
    return f"test_{basename}.py"


def _resolve_test_path(
    changed_file: str,
    repo_root: Path,
    *,
    existing_paths: Optional[Set[str]] = None,
) -> Optional[str]:
    """Map ONE changed source file to its best test path.

    If ``existing_paths`` is provided (a set of relative repo paths that
    actually exist), existence checks are O(1) set lookups; otherwise we
    stat each candidate on disk.  This injection point makes the function
    fully offline-testable.

    Returns a path relative to ``repo_root``, or None when no test is found.
    """
    changed_file = changed_file.lstrip("/")

    # Step 1: find the matching source→test prefix.
    matched_prefix = None
    test_prefix = None
    for src_prefix, tst_prefix in SRC_TO_TEST_PREFIX:
        if changed_file.startswith(src_prefix):
            matched_prefix = src_prefix
            test_prefix = tst_prefix
            break

    if matched_prefix is None or test_prefix is None:
        # This file doesn't match any known source prefix (e.g. top-level
        # __init__.py).  Return None to signal "no test mapping".
        return None

    relative = changed_file[len(matched_prefix):]  # everything after the prefix
    if not relative:
        # The changed file IS the directory root (e.g. "scripts/" with no sub-path).
        return None

    basename = relative.split("/")[-1]  # last path component
    test_basename = _basename_to_test_basename(basename)

    # The test dir is the same subdirectory structure under tests/
    subdir = relative.rsplit("/", 1)[0] if "/" in relative else ""
    if subdir:
        test_subdir = f"{subdir}/"
    else:
        test_subdir = ""

    # ── Tier 1: Exact match ─────────────────────────────────────────────────
    exact_candidate = f"{test_prefix}{test_subdir}{test_basename}"
    if _path_exists(exact_candidate, repo_root, existing_paths):
        return exact_candidate

    # ── Tier 3: Directory fallback ───────────────────────────────────────────
    # The directory containing the test — use the nearest test directory.
    dir_candidate = f"{test_prefix}{test_subdir}" if test_subdir else test_prefix.rstrip("/")
    if _path_exists(dir_candidate, repo_root, existing_paths):
        return dir_candidate

    # ── Walk up: if the exact subdir doesn't exist (e.g. the source module
    #    has no corresponding test directory), try the parent.
    if test_subdir:
        dir_candidate = test_prefix.rstrip("/")
        if _path_exists(dir_candidate, repo_root, existing_paths):
            return dir_candidate

    return None


def _path_exists(
    rel_path: str,
    repo_root: Path,
    existing_paths: Optional[Set[str]] = None,
) -> bool:
    """Check whether a relative path (file or directory) exists.

    When ``existing_paths`` is a set of *file* paths, directory checks use
    prefix-matching: a directory ``d/`` "exists" if at least one file in the
    set starts with ``d/``.
    """
    if existing_paths is not None:
        if rel_path in existing_paths:
            return True
        # Directory check: does any file live inside this directory?
        if rel_path.endswith("/"):
            rel_path = rel_path.rstrip("/")
        for p in existing_paths:
            if p == rel_path or p.startswith(rel_path + "/"):
                return True
        return False
    return (repo_root / rel_path).exists()


def map_changed_files_to_shards(
    changed_files: Sequence[str],
    repo_root: Path,
    *,
    existing_paths: Optional[Set[str]] = None,
) -> List[TestShard]:
    """Map a list of changed source files to deduplicated test shards.

    Each changed file may resolve to a test path (exact match) or a test
    directory.  Shards are deduplicated by their pytest argument string.
    If no source files map to any test, returns an empty list.
    """
    seen: Set[str] = set()
    shards: List[TestShard] = []

    for f in changed_files:
        test_path = _resolve_test_path(f, repo_root, existing_paths=existing_paths)
        if test_path is None:
            continue

        # Determine if it's a file or directory
        if _is_file(test_path, repo_root, existing_paths):
            pytest_args = [test_path, "-x", "-q", f"--timeout={DEFAULT_TIMEOUT}"]
            desc = f"exact: {test_path}"
        else:
            pytest_args = [test_path, "-x", "-q", f"--timeout={DEFAULT_TIMEOUT}"]
            desc = f"dir: {test_path}"

        key = " ".join(pytest_args)
        if key not in seen:
            seen.add(key)
            shards.append(TestShard(pytest_args=pytest_args, description=desc))

    return shards


def _is_file(
    rel_path: str,
    repo_root: Path,
    existing_paths: Optional[Set[str]] = None,
) -> bool:
    if existing_paths is not None:
        return rel_path in existing_paths
    return (repo_root / rel_path).is_file()


# ── Runner ──────────────────────────────────────────────────────────────────────

def run_shard(
    shard: TestShard,
    repo_root: Path,
    *,
    extra_env: Optional[Dict[str, str]] = None,
    runner: Optional[Callable] = None,
) -> TestResult:
    """Run a single test shard and return the result.

    ``runner`` is injectable for tests (defaults to subprocess.run).
    """
    cmd = shard.as_cmd()
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)

    if runner is not None:
        t0 = time.monotonic()
        rc, stdout, stderr = runner(cmd, env)
        elapsed = time.monotonic() - t0
        return TestResult(
            shard=shard,
            returncode=rc,
            stdout=stdout,
            stderr=stderr,
            elapsed_sec=elapsed,
        )

    t0 = time.monotonic()
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=str(repo_root),
        env=env,
    )
    elapsed = time.monotonic() - t0
    return TestResult(
        shard=shard,
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
        elapsed_sec=elapsed,
    )


def get_fallback_shard() -> TestShard:
    """Last-resort shard when no specific test files are found."""
    return TestShard(
        pytest_args=[
            "tests/", "-x", "-q", f"--timeout={DEFAULT_FALLBACK_TIMEOUT}",
            "-k", DEFAULT_FALLBACK_KWARGS,
        ],
        description="fallback: tests/ (filtered)",
    )


# ── Logging ─────────────────────────────────────────────────────────────────────

def _log_dir() -> Path:
    return Path(os.path.expanduser(_LOG_DIR_TEMPLATE))


def write_log(report: GateReport, log_path: Optional[Path] = None) -> Path:
    """Write the full gate report to a timestamped log file.

    Returns the path to the written log file.
    """
    if log_path is None:
        ts = time.strftime("%Y%m%dT%H%M%S")
        log_path = _log_dir() / f"{ts}.log"

    log_path.parent.mkdir(parents=True, exist_ok=True)

    lines: List[str] = []
    lines.append(f"=== Pre-PR Test Runner — Gate Report ===")
    lines.append(f"Time: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"Changed files ({len(report.changed_files)}):")
    for cf in report.changed_files:
        lines.append(f"  {cf}")
    lines.append("")

    if report.shards:
        lines.append(f"Test shards ({len(report.shards)}):")
        for s in report.shards:
            lines.append(f"  [{s.description}] {' '.join(s.pytest_args)}")
    else:
        lines.append("No test shards identified.")
    lines.append("")

    for r in report.results:
        lines.append(f"--- Shard: {r.shard.description} ---")
        lines.append(f"Command: {' '.join(r.shard.as_cmd())}")
        lines.append(f"Exit code: {r.returncode}")
        lines.append(f"Elapsed: {r.elapsed_sec:.2f}s")
        lines.append("")
        if r.stdout:
            lines.append("[stdout]")
            for line in r.stdout.splitlines():
                lines.append(f"  {line}")
            lines.append("")
        if r.stderr:
            lines.append("[stderr]")
            for line in r.stderr.splitlines():
                lines.append(f"  {line}")
            lines.append("")

    lines.append(f"=== Overall: {'PASSED' if report.passed else 'FAILED'} ===")
    if report.note:
        lines.append(f"Note: {report.note}")

    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return log_path


# ── Main orchestrator (pure core, testable) ─────────────────────────────────────

def run_gate(
    changed_files: Sequence[str],
    repo_root: Path,
    *,
    existing_paths: Optional[Set[str]] = None,
    runner: Optional[Callable] = None,
    log_path: Optional[Path] = None,
) -> GateReport:
    """Core gate logic: map, run, report. Pure except for subprocess + log IO.

    Returns a ``GateReport`` whose ``.passed`` is True when all shards pass.
    """
    report = GateReport(changed_files=list(changed_files))

    if not changed_files:
        report.passed = True
        report.note = "no changed files — nothing to test"
        return report

    shards = map_changed_files_to_shards(
        changed_files, repo_root, existing_paths=existing_paths
    )

    if not shards:
        # No specific test shards found — fall back to the last-resort full run.
        fallback = get_fallback_shard()
        shards = [fallback]
        report.note = "no specific test shards found; falling back to filtered full suite"

    report.shards = shards

    all_passed = True
    for shard in shards:
        result = run_shard(shard, repo_root, runner=runner)
        report.results.append(result)
        if result.returncode != 0:
            all_passed = False

    report.passed = all_passed
    if not all_passed:
        failed = [r for r in report.results if r.returncode != 0]
        report.note = f"{len(failed)}/{len(report.results)} shard(s) failed"

    write_log(report, log_path=log_path)
    return report


# ── CLI ─────────────────────────────────────────────────────────────────────────

def _parse_changed_files_arg(value: Optional[str]) -> List[str]:
    """Parse the --changed-files comma-separated argument."""
    if not value:
        return []
    return [f.strip() for f in value.split(",") if f.strip()]


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Pre-PR local test runner: run targeted tests for changed files."
    )
    parser.add_argument(
        "--changed-files",
        type=str,
        default=None,
        help="Comma-separated list of changed file paths (relative to repo root)",
    )
    parser.add_argument(
        "--changed-files-file",
        type=str,
        default=None,
        help="Path to a file containing one changed file path per line",
    )
    parser.add_argument(
        "--repo-root",
        type=str,
        default=None,
        help="Repository root directory (default: auto-detected via git)",
    )
    parser.add_argument(
        "--log-dir",
        type=str,
        default=None,
        help="Directory for test result logs (default: ~/.hermes/evolution/pre-pr-test-results/)",
    )
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        default=False,
        help="Stop after the first failing shard (default: run all shards)",
    )

    args = parser.parse_args(argv)

    # Resolve changed files
    changed_files = _parse_changed_files_arg(args.changed_files)
    if args.changed_files_file:
        p = Path(args.changed_files_file)
        if p.exists():
            file_content = p.read_text(encoding="utf-8").strip()
            for line in file_content.splitlines():
                stripped = line.strip()
                if stripped and not stripped.startswith("#"):
                    changed_files.append(stripped)
        else:
            print(f"ERROR: --changed-files-file {args.changed_files_file} not found",
                  file=sys.stderr)
            return 2

    if not changed_files:
        # Auto-detect from git diff
        try:
            proc = subprocess.run(
                ["git", "diff", "--name-only", "HEAD"],
                capture_output=True, text=True, cwd=args.repo_root or ".",
            )
            if proc.returncode == 0:
                for line in proc.stdout.splitlines():
                    stripped = line.strip()
                    if stripped and stripped.endswith(".py"):
                        changed_files.append(stripped)
        except Exception:
            pass

    if not changed_files:
        print("No changed files found. Nothing to test.", file=sys.stderr)
        return 0

    # Resolve repo root
    if args.repo_root:
        repo_root = Path(args.repo_root).resolve()
    else:
        # Auto-detect
        try:
            proc = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                capture_output=True, text=True,
            )
            repo_root = Path(proc.stdout.strip()).resolve()
        except Exception:
            repo_root = Path.cwd()

    if not repo_root.exists():
        print(f"ERROR: repo root {repo_root} does not exist", file=sys.stderr)
        return 2

    # Resolve log dir
    if args.log_dir:
        log_dir = Path(args.log_dir)
    else:
        log_dir = _log_dir()

    ts = time.strftime("%Y%m%dT%H%M%S")
    log_path = log_dir / f"{ts}.log"

    report = run_gate(changed_files, repo_root, log_path=log_path)

    print(f"\n{'='*60}")
    print(f"Pre-PR Test Runner — {'PASSED' if report.passed else 'FAILED'}")
    print(f"Changed files: {len(report.changed_files)}")
    print(f"Test shards:   {len(report.shards)}")
    for r in report.results:
        status = "PASS" if r.returncode == 0 else f"FAIL (exit {r.returncode})"
        print(f"  [{status}] {r.shard.description} ({r.elapsed_sec:.1f}s)")
    if report.note:
        print(f"Note: {report.note}")
    print(f"Log: {log_path}")
    print(f"{'='*60}")

    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
