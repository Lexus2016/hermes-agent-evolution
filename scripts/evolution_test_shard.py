#!/usr/bin/env python3
"""Map edited source files to the most relevant test shard for pre-PR validation.

This is the deterministic, import-safe helper behind the evolution
implementation stage's pre-PR local-test gate (issue #580).  Given a list of
changed file paths (relative to the repo root), it returns:

  * a concrete pytest invocation that exercises the affected code,
  * the list of test files selected,
  * the heuristic used.

Why a separate script?
- Keeps the skill's decision logic in deterministic, unit-testable Python.
- Avoids adding new core model tools; the skill simply runs this via the
  existing ``terminal`` toolset.
- The mapping can evolve (more languages, finer heuristics) without touching
  the skill prose.

Heuristic
---------
1. Tests live under ``tests/`` and mirror the source layout, or are named
   ``test_<module>.py`` / ``test_<module>_<suffix>.py`` next to the module.
2. For each changed source file under the repo root, look for:
   a) a test file at the mirrored path ``tests/<dir>/test_<stem>.py``,
   b) any ``test_*<stem>*.py`` file in the same directory as the source,
   c) any ``test_*<stem>*.py`` file in ``tests/<dir>/``.
3. If no test file is found for a source file, fall back to the broadest
   affected directory under ``tests/`` (e.g. ``tests/agent/`` for
   ``agent/foo.py``).
4. Deduplicate and return the smallest useful command.

CLI
---
    evolution_test_shard.py <changed-file> [<changed-file> ...]

Prints JSON and exits 0 when a shard is found, 1 when nothing maps.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


def _split_path_parts(path: str) -> List[str]:
    return [p for p in path.replace("\\", "/").split("/") if p]


def _stem(path: str) -> str:
    return Path(path).stem


def _is_source_file(path: str) -> bool:
    p = Path(path)
    if p.suffix != ".py":
        return False
    parts = _split_path_parts(path)
    # Skip tests themselves, build dirs, and hidden dirs.
    if any(part.startswith(".") for part in parts):
        return False
    if "tests" in parts:
        return False
    if "__pycache__" in parts:
        return False
    return True


def _mirrored_test(path: str) -> Optional[str]:
    """``agent/foo.py`` -> ``tests/agent/test_foo.py``."""
    parts = _split_path_parts(path)
    if not parts:
        return None
    stem = _stem(path)
    return os.path.join("tests", *parts[:-1], f"test_{stem}.py")


def _tests_in_dir(directory: Path, stem: str, repo_root: Path) -> List[str]:
    """Find ``test_*<stem>*.py`` files in ``directory``."""
    if not directory.is_dir():
        return []
    pattern = f"test_*{stem}*.py"
    return sorted(str(p.relative_to(repo_root)) for p in directory.glob(pattern))


def _collect_candidates(path: str, repo_root: Path) -> Tuple[List[str], List[str]]:
    """Return (test_files, fallback_dirs) for one source path.

    ``fallback_dirs`` are broad test directories to use when no specific test file
    is found.
    """
    stem = _stem(path)
    parts = _split_path_parts(path)

    candidates: List[str] = []

    # 1. Mirrored layout.
    mirrored = _mirrored_test(path)
    if mirrored:
        resolved = repo_root / mirrored
        if resolved.is_file():
            candidates.append(mirrored)

    # 2. Tests in the source's own directory (rare, but allowed for scripts).
    if parts:
        src_dir = repo_root / os.path.join(*parts[:-1])
        candidates.extend(_tests_in_dir(src_dir, stem, repo_root))

    # 3. Tests under the mirrored directory.
    if len(parts) > 1:
        test_dir = repo_root / "tests" / os.path.join(*parts[:-1])
    else:
        test_dir = repo_root / "tests"
    candidates.extend(_tests_in_dir(test_dir, stem, repo_root))

    # Fallback directories.
    fallbacks: List[str] = []
    if parts:
        # If the source is in a package, map to tests/<package>/.
        candidate_dir = repo_root / "tests" / parts[0]
        if candidate_dir.is_dir():
            fallbacks.append(str(candidate_dir.relative_to(repo_root)))
        # One level deeper if available.
        if len(parts) > 1:
            deeper = repo_root / "tests" / parts[0] / parts[1]
            if deeper.is_dir():
                fallbacks.append(str(deeper.relative_to(repo_root)))

    return candidates, fallbacks


def _uniq(seq: Sequence[str]) -> List[str]:
    """Deduplicate while preserving order."""
    seen = set()
    out: List[str] = []
    for s in seq:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def build_shard(
    changed_files: Sequence[str],
    repo_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Build a test shard from a list of changed file paths.

    Returns a dict with keys:
      - command: list of argv for the test runner,
      - test_files: list of concrete test files selected,
      - fallback_dirs: broad test dirs used for files without specific tests,
      - heuristic: human-readable summary of how the shard was built,
      - changed: the source files that contributed.
    """
    repo_root = repo_root or Path.cwd()
    changed_files = [str(f) for f in changed_files]

    source_files = [p for p in changed_files if _is_source_file(p)]
    # If the PR only touches tests, run those directly.
    if not source_files:
        test_only = [
            p
            for p in changed_files
            if p.endswith(".py") and "tests" in _split_path_parts(p)
        ]
        if test_only:
            return {
                "command": ["python", "-m", "pytest", *test_only, "-q"],
                "test_files": test_only,
                "fallback_dirs": [],
                "heuristic": "test-only change: run the edited test files",
                "changed": changed_files,
            }

    test_files: List[str] = []
    fallback_dirs: List[str] = []

    for path in source_files:
        cands, falls = _collect_candidates(path, repo_root)
        if cands:
            test_files.extend(cands)
        if falls:
            fallback_dirs.extend(falls)

    # Deduplicate while preserving order.
    test_files = _uniq(test_files)
    fallback_dirs = _uniq(fallback_dirs)

    # Filter out paths that don't exist in this repo_root (e.g. a source file's
    # own directory may have produced pattern candidates that don't exist here).
    test_files = [t for t in test_files if (repo_root / t).is_file()]
    fallback_dirs = [d for d in fallback_dirs if (repo_root / d).is_dir()]

    # If we have concrete test files for a source directory, don't also schedule
    # the whole directory as a fallback.
    covered_dirs = {
        str(Path(t).parts[0] + "/" + Path(t).parts[1])
        for t in test_files
        if len(Path(t).parts) >= 2
    }
    fallback_dirs = [d for d in fallback_dirs if d not in covered_dirs]

    # Prefer concrete test files; fall back to directory shards only when needed.
    targets = test_files or fallback_dirs
    if not targets:
        return {
            "command": [],
            "test_files": [],
            "fallback_dirs": [],
            "heuristic": "no mapped tests found",
            "changed": source_files,
        }

    if test_files:
        heuristic = f"mirrored/nearby tests for {len(source_files)} source file(s)"
    else:
        heuristic = f"directory fallback for {len(source_files)} source file(s)"

    return {
        "command": ["python", "-m", "pytest", *targets, "-q"],
        "test_files": test_files,
        "fallback_dirs": fallback_dirs,
        "heuristic": heuristic,
        "changed": source_files,
    }


def _find_changed_files(git_root: Path) -> List[str]:
    """Read ``git diff --name-only HEAD`` to discover changed files."""
    import subprocess

    try:
        proc = subprocess.run(
            ["git", "-C", str(git_root), "diff", "--name-only", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except Exception:
        return []
    if proc.returncode != 0:
        return []
    return [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]


def main(argv: List[str]) -> int:
    repo_root = Path(os.environ.get("EVOLUTION_REPO_DIR", str(Path.cwd())))

    # If no positional args, infer from git diff.
    if len(argv) < 2:
        changed_files = _find_changed_files(repo_root)
    else:
        changed_files = argv[1:]

    shard = build_shard(changed_files, repo_root)
    print(json.dumps(shard, indent=2))
    return 0 if shard["command"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
