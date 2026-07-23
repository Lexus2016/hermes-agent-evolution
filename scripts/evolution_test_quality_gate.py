#!/usr/bin/env python3
"""Test-quality gates for the evolution merge/implementation pipeline.

Two deterministic, LLM-free quality checks that address the root cause of the
18-merge miss streak (REALIZED_IMPACT_LOW): the merge gate was shipping
confidence instead of correctness because agent-generated tests could pass
without actually verifying real behavior.

**#1209 — fabricated-reproduction detection.**  Dan Luu's analysis of
agentic coding shows agents can produce convincing-looking but staged test
reproductions — tests that only pass in the agent's constructed environment.
This module detects the structural patterns of fabricated reproductions:
tests whose entire assertion surface is mocked, tests that patch out the
function-under-test, and tests whose pass condition depends on a
dynamically-constructed mock chain rather than real I/O.

**#1210 — mock-ratio quality gate.**  The MSR 2026 study (Hora & Robbes,
1.2M+ commits, 2,168 repos) finds agents over-mock: 36% of agent test
commits add mocks vs 26% for humans.  A high mock ratio means tests pass
without exercising real code paths.  This module computes the ratio of
mock-adding test changes to total test changes in a PR and flags PRs
exceeding a configurable threshold (default 30%).

Both are pure functions + explicit IO so they are import-safe and
unit-testable.  The CLI entry point orchestrates git-diff / file reads.

Integration points:
  * ``check_merge_policy`` in ``evolution_merge_gate.py`` can call
    ``detect_fabricated_reproduction`` and ``compute_mock_ratio`` to add
    blocking violations.
  * ``compute_funnel`` in ``evolution_funnel.py`` can append
    ``mock_ratio`` to ``metrics.jsonl`` for longitudinal tracking.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ── #1209: Fabricated-reproduction detection ────────────────────────────────

# Patterns that indicate a test is structurally fabricated — its pass
# condition depends on the agent's constructed environment rather than real
# behavior.  Each pattern is a compiled regex matched against test file
# *content* (not just file names).
_FABRICATED_PATTERNS: Tuple[Tuple[re.Pattern, str], ...] = (
    # Patches out the function/class under test itself — the test never
    # calls the real implementation.
    (
        re.compile(
            r"patch\s*\(\s*['\"]?[\w.]*test_",  # patch('...test_...
            re.IGNORECASE,
        ),
        "PATCHES_TEST_SELF: test patches out the function-under-test — "
        "pass condition does not exercise real code",
    ),
    # Every assertion uses a mocked return value, never a real computation.
    (
        re.compile(
            r"assert\s+\w+\.return_value",  # assert x.return_value
            re.IGNORECASE,
        ),
        "ASSERTS_RETURN_VALUE: assertions check mock.return_value, not real "
        "computation output",
    ),
    # Test body is entirely mock.setup + mock.assert-called — no real I/O.
    (
        re.compile(
            r"def\s+test_\w+\(.*\).*:\s*\n"
            r"(?:\s*(?:mock|patch|MagicMock|create_autospec)\b.*){3,}",
            re.MULTILINE,
        ),
        "ALL_MOCK_BODY: test body is 3+ consecutive mock/patch lines with "
        "no real invocation — fabricated environment",
    ),
)

# Tests that exercise real behavior despite containing some mocks (e.g.,
# integration tests that mock only an external API).  These patterns
# counter-signal fabrication.
_REAL_BEHAVIOR_PATTERNS: Tuple[re.Pattern, ...] = (
    re.compile(r"\bsubprocess\.(run|call|Popen)\b"),  # real process exec
    re.compile(
        r"\brequests\.(get|post|put|delete)\b"
    ),  # real HTTP (even if mocked at transport)
    re.compile(r"\bPath\s*\(\s*['\"]?/", re.IGNORECASE),  # real filesystem path
    re.compile(r"\bopen\s*\("),  # real file I/O
    re.compile(r"\btmp_path\b"),  # pytest real temp directory fixture
    re.compile(r"\btmpdir\b"),  # legacy pytest real temp fixture
)


@dataclass
class FabricationFinding:
    """A single fabrication signal found in a test file."""

    file_path: str
    pattern_name: str
    description: str
    line_number: int = 0


@dataclass
class FabricationReport:
    """Result of fabricated-reproduction detection across a set of test files."""

    findings: List[FabricationFinding] = field(default_factory=list)
    files_scanned: int = 0
    files_flagged: int = 0

    @property
    def is_flagged(self) -> bool:
        """True when at least one test file shows fabrication patterns."""
        return bool(self.findings)

    def summary(self) -> str:
        if not self.findings:
            return f"CLEAN: {self.files_scanned} test files scanned, no fabrication signals"
        names = sorted({f.pattern_name for f in self.findings})
        return (
            f"FLAGGED: {self.files_flagged}/{self.files_scanned} test files show "
            f"fabrication patterns ({', '.join(names)})"
        )


def detect_fabricated_reproduction(
    test_contents: Dict[str, str],
) -> FabricationReport:
    """Detect fabricated test reproductions in a set of test files.

    ``test_contents`` maps repo-relative file path → full file content.
    Only files whose path looks like a test file (``test_*.py`` /
    ``*_test.py``) are scanned.

    Returns a :class:`FabricationReport` with per-file findings.  A file is
    flagged only when it matches a fabrication pattern AND does not contain
    any real-behavior counter-signal (integration tests that mock an
    external API while exercising real code are NOT flagged).
    """
    report = FabricationReport()
    for path, content in test_contents.items():
        if not _is_test_file(path):
            continue
        report.files_scanned += 1
        if not isinstance(content, str):
            continue
        has_real_behavior = any(p.search(content) for p in _REAL_BEHAVIOR_PATTERNS)
        flagged_this_file = False
        for i, line in enumerate(content.splitlines(), start=1):
            for pattern, description in _FABRICATED_PATTERNS:
                if pattern.search(line):
                    # Don't flag if the test also exercises real behavior —
                    # a mock-heavy integration test is NOT a fabricated
                    # reproduction.
                    if has_real_behavior:
                        continue
                    name = description.split(":")[0]
                    report.findings.append(
                        FabricationFinding(
                            file_path=path,
                            pattern_name=name,
                            description=description,
                            line_number=i,
                        )
                    )
                    flagged_this_file = True
        if flagged_this_file:
            report.files_flagged += 1
    return report


def _is_test_file(path: str) -> bool:
    """Heuristic: is this a Python test file?"""
    p = path.lower()
    return p.endswith(".py") and (
        "/test_" in p or p.startswith("test_") or "_test.py" in p or "/tests/" in p
    )


# ── #1210: Mock-ratio quality gate ──────────────────────────────────────────

# Patterns that indicate a *line* in a test diff adds a mock.  Matched
# against added lines (``+``-prefixed in unified diff).
_MOCK_ADD_PATTERNS: Tuple[re.Pattern, ...] = (
    re.compile(r"^\+\s*(?:from\s+)?(?:unittest\.)?mock\b", re.IGNORECASE),
    re.compile(r"^\+\s*from\s+unittest\.mock\s+import\b", re.IGNORECASE),
    re.compile(
        r"^\+\s*(?:mock|patch|MagicMock|Mock|create_autospec)\s*[\(=]", re.IGNORECASE
    ),
    re.compile(r"^\+\s*@patch\b", re.IGNORECASE),
    re.compile(r"^\+\s*@.*\.mock\b", re.IGNORECASE),
    re.compile(r"^\+\s*.*\bpatch\.object\s*\(", re.IGNORECASE),
    re.compile(r"^\+\s*.*\bmonkeypatch\b", re.IGNORECASE),  # pytest monkeypatch fixture
)

# Threshold: PRs where more than this fraction of test changes add mocks are
# flagged.  Default 30% per the MSR 2026 finding (36% agent vs 26% human).
DEFAULT_MOCK_RATIO_THRESHOLD = 0.30


@dataclass
class MockRatioResult:
    """Result of mock-ratio analysis on a PR's test changes."""

    mock_adding_test_files: int = 0
    total_test_files: int = 0
    mock_ratio: float = 0.0  # mock_adding / total, 0..1
    threshold: float = DEFAULT_MOCK_RATIO_THRESHOLD
    flagged: bool = False
    flagged_files: List[str] = field(default_factory=list)

    def summary(self) -> str:
        status = "FLAGGED" if self.flagged else "OK"
        if self.total_test_files == 0:
            return f"{status}: no test files in PR — mock ratio not applicable"
        return (
            f"{status}: mock_ratio={self.mock_ratio:.0%} "
            f"({self.mock_adding_test_files}/{self.total_test_files} test files add mocks, "
            f"threshold={self.threshold:.0%})"
        )


def _is_test_path(path: str) -> bool:
    """Check if a changed file path is a test file (broader than _is_test_file)."""
    return _is_test_file(path)


def compute_mock_ratio(
    files: Sequence[Dict[str, Any]],
    threshold: float = DEFAULT_MOCK_RATIO_THRESHOLD,
) -> MockRatioResult:
    """Compute the mock ratio for a PR's changed files.

    ``files`` is the ``gh pr view --json files`` shape:
    ``[{\"path\": str, \"additions\": int, \"deletions\": int}, ...]``.

    A test file is \"mock-adding\" when its additions > 0 AND the file path
    or the patch content indicates mock imports/calls were added.  Since the
    ``gh pr view --json files`` shape does not include patch content, this
    function uses a conservative heuristic: a test file is counted as
    mock-adding if its path or name suggests mock usage OR if the caller
    provides patch content via the ``patches`` parameter.

    For the ``gh pr view --json files`` path (no patch content), the mock
    ratio is computed from file-name heuristics only — this is intentionally
    conservative and may undercount.  For the ``--with-patches`` path (via
    :func:`compute_mock_ratio_from_diff`), the ratio is computed from actual
    added lines, which is precise.
    """
    result = MockRatioResult(threshold=threshold)
    test_files = [
        f
        for f in files
        if isinstance(f, dict) and _is_test_path(str(f.get("path", "")))
    ]
    result.total_test_files = len(test_files)
    if result.total_test_files == 0:
        return result

    for f in test_files:
        path = str(f.get("path", ""))
        additions = int(f.get("additions") or 0)
        if additions <= 0:
            continue
        # Conservative heuristic: flag based on mock-related patterns in
        # file content if available, else just count the test file.
        patch = f.get("patch") or ""
        if patch and _diff_adds_mocks(patch):
            result.mock_adding_test_files += 1
            result.flagged_files.append(path)
        elif not patch and _path_suggests_mocks(path):
            # No patch content — use filename heuristic (rarely matches,
            # so this is very conservative / undercounts).
            result.mock_adding_test_files += 1
            result.flagged_files.append(path)

    result.mock_ratio = (
        result.mock_adding_test_files / result.total_test_files
        if result.total_test_files > 0
        else 0.0
    )
    result.flagged = result.mock_ratio > threshold
    return result


def compute_mock_ratio_from_diff(
    diff: str,
    threshold: float = DEFAULT_MOCK_RATIO_THRESHOLD,
) -> MockRatioResult:
    """Compute mock ratio from a unified diff string (precise).

    Parses the diff to identify test files and whether their added lines
    contain mock-related imports/calls.  This is the precise path used when
    patch content is available (e.g., from ``git diff``).
    """
    result = MockRatioResult(threshold=threshold)
    if not diff or not isinstance(diff, str):
        return result

    # Split into per-file sections (diff --git ...)
    sections = re.split(r"^diff --git ", diff, flags=re.MULTILINE)
    for section in sections[1:]:  # skip pre-first-split empty string
        # Extract file path from "a/path b/path" header
        m = re.match(r"a/(\S+)\s+b/(\S+)", section)
        if not m:
            continue
        path = m.group(2)
        if not _is_test_path(path):
            continue
        result.total_test_files += 1
        if _diff_adds_mocks(section):
            result.mock_adding_test_files += 1
            result.flagged_files.append(path)

    result.mock_ratio = (
        result.mock_adding_test_files / result.total_test_files
        if result.total_test_files > 0
        else 0.0
    )
    result.flagged = result.mock_ratio > threshold
    return result


def _diff_adds_mocks(diff_section: str) -> bool:
    """Check whether a diff section adds mock-related lines."""
    for line in diff_section.splitlines():
        for pattern in _MOCK_ADD_PATTERNS:
            if pattern.match(line):
                return True
    return False


def _path_suggests_mocks(path: str) -> bool:
    """Conservative: does the file *name* suggest mocks? (very low false-positive).

    Only matches files explicitly named for mocking — e.g. ``test_mock_*``
    or ``mock_*test*``.  This is intentionally narrow to avoid false
    positives when patch content is unavailable.
    """
    p = path.lower()
    return "test_mock_" in p or "/mock_" in p or "mock_test" in p


# ── Merge-gate integration ──────────────────────────────────────────────────


def check_test_quality(
    files: Sequence[Dict[str, Any]],
    test_contents: Optional[Dict[str, str]] = None,
    mock_ratio_threshold: float = DEFAULT_MOCK_RATIO_THRESHOLD,
) -> List[str]:
    """Return blocking-violation strings from both quality gates (empty == OK).

    This is the single entry point for merge-gate integration: call this
    alongside ``check_merge_policy`` to add test-quality violations.

    ``files`` — the ``gh pr view --json files`` shape.
    ``test_contents`` — optional map of test file path → content for
    fabricated-reproduction detection.  When None, only the mock-ratio
    gate runs (fabrication detection requires file content).
    """
    violations: List[str] = []

    # Mock-ratio gate (#1210)
    mr = compute_mock_ratio(files, threshold=mock_ratio_threshold)
    if mr.flagged:
        violations.append(
            f"HIGH_MOCK_RATIO: {mr.mock_ratio:.0%} of test files add mocks "
            f"({mr.mock_adding_test_files}/{mr.total_test_files}, threshold "
            f"{mr.threshold:.0%}) — over-mocked tests give false confidence; "
            f"prefer integration tests"
        )

    # Fabricated-reproduction gate (#1209)
    if test_contents:
        fr = detect_fabricated_reproduction(test_contents)
        if fr.is_flagged:
            violations.append(
                f"FABRICATED_REPRODUCTION: {fr.summary()} — tests that only "
                f"pass in constructed environments cannot verify real behavior"
            )

    return violations


# ── CLI ─────────────────────────────────────────────────────────────────────


def _run(cmd: List[str]) -> Tuple[int, str, str]:
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


def _pr_diff(pr: int, repo: Optional[str]) -> Optional[str]:
    """Get the unified diff for a PR via gh."""
    cmd = ["gh", "pr", "diff", str(pr)]
    if repo:
        cmd += ["--repo", repo]
    code, out, err = _run(cmd)
    if code != 0:
        return None
    return out


def _pr_files(pr: int, repo: Optional[str]) -> Optional[List[Dict[str, Any]]]:
    cmd = ["gh", "pr", "view", str(pr), "--json", "files"]
    if repo:
        cmd += ["--repo", repo]
    code, out, _ = _run(cmd)
    if code != 0:
        return None
    try:
        return json.loads(out).get("files") or []
    except (ValueError, KeyError):
        return None


def _read_test_files(file_paths: List[str], base: str = "HEAD") -> Dict[str, str]:
    """Read file contents at a given git ref for fabrication detection."""
    contents: Dict[str, str] = {}
    for path in file_paths:
        if not _is_test_file(path):
            continue
        code, out, _ = _run(["git", "show", f"{base}:{path}"])
        if code == 0 and out:
            contents[path] = out
    return contents


def main(argv: List[str]) -> int:
    args = argv[1:]
    if "--pr" not in args:
        print(
            "usage: evolution_test_quality_gate.py --pr N "
            "[--mock-ratio-threshold 0.30] [--repo O/R] [--check-only]"
        )
        return 2
    pr = int(args[args.index("--pr") + 1])
    repo = (
        args[args.index("--repo") + 1]
        if "--repo" in args
        else os.environ.get("EVOLUTION_REPO_SLUG")
    )
    threshold = DEFAULT_MOCK_RATIO_THRESHOLD
    if "--mock-ratio-threshold" in args:
        try:
            threshold = float(args[args.index("--mock-ratio-threshold") + 1])
        except (ValueError, IndexError):
            pass

    # Get PR files
    files = _pr_files(pr, repo)
    if files is None:
        print(f"[test-quality] could not read PR #{pr} files — skipping")
        return 0  # fail-open: don't block on gh errors

    # Get PR diff for precise mock-ratio
    diff = _pr_diff(pr, repo)

    # Run mock-ratio gate (precise path if diff available)
    if diff:
        mr = compute_mock_ratio_from_diff(diff, threshold=threshold)
    else:
        mr = compute_mock_ratio(files, threshold=threshold)
    print(f"[test-quality] mock-ratio: {mr.summary()}")

    # Run fabricated-reproduction gate (requires test file contents)
    test_paths = [
        str(f.get("path", "")) for f in files if _is_test_file(str(f.get("path", "")))
    ]
    test_contents = _read_test_files(test_paths)
    fr = detect_fabricated_reproduction(test_contents)
    if fr.files_scanned > 0:
        print(f"[test-quality] fabrication: {fr.summary()}")
    else:
        print("[test-quality] fabrication: no test files to scan")

    # Output JSON for pipeline integration
    result = {
        "pr": pr,
        "mock_ratio": round(mr.mock_ratio, 4),
        "mock_adding_test_files": mr.mock_adding_test_files,
        "total_test_files": mr.total_test_files,
        "mock_ratio_flagged": mr.flagged,
        "fabrication_findings": len(fr.findings),
        "fabrication_files_flagged": fr.files_flagged,
        "fabrication_flagged": fr.is_flagged,
    }
    print(f"[test-quality] json: {json.dumps(result, sort_keys=True)}")

    # Exit 1 if either gate flagged (unless --check-only, which just reports)
    if "--check-only" not in args:
        if mr.flagged or fr.is_flagged:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
