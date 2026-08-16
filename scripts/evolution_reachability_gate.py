#!/usr/bin/env python3
"""Deterministic dead-code reachability gate for evolution review and self-merge (#2498).

Verifies that new top-level symbols (classes, functions) introduced by a PR in
production code are actually reached by at least one production (non-test) call site
outside their defining module.

Prevents green-but-dead code (e.g. #2476, #2474, #2490, #2491) from landing unnoticed.
"""

from __future__ import annotations

import ast
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple


def _is_test_file(path: str) -> bool:
    """Predicate identifying test files."""
    p = path.replace("\\", "/").lower()
    base = p.rsplit("/", 1)[-1]
    return (
        p.startswith("tests/")
        or "/tests/" in p
        or base.startswith("test_")
        or base.endswith("_test.py")
    )


def _is_source_python_file(path: str) -> bool:
    """Predicate identifying non-test Python source files."""
    p = path.replace("\\", "/").lower()
    if not p.endswith(".py"):
        return False
    if _is_test_file(p):
        return False
    return True


def extract_top_level_symbols(code: str) -> List[str]:
    """Extract public top-level class and function names from Python source code."""
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError):
        return []

    symbols: List[str] = []
    ignored_names = {"main", "cli", "run", "app"}
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            name = node.name
            if not name.startswith("_") and name not in ignored_names:
                symbols.append(name)
    return symbols


def find_symbol_references(
    symbol: str,
    defining_file: str,
    all_files: Dict[str, str],
) -> List[str]:
    """Find all non-test files in all_files that reference symbol outside defining_file."""
    pattern = re.compile(rf"\b{re.escape(symbol)}\b")
    norm_def = defining_file.replace("\\", "/").lower()
    if norm_def.startswith("./"):
        norm_def = norm_def[2:]

    referencing_files: List[str] = []
    for file_path, content in all_files.items():
        norm_path = file_path.replace("\\", "/").lower()
        if norm_path.startswith("./"):
            norm_path = norm_path[2:]
        if norm_path == norm_def:
            continue
        if _is_test_file(norm_path):
            continue
        if pattern.search(content):
            referencing_files.append(file_path)

    return referencing_files


def check_reachability(
    files: Sequence[Dict[str, Any]],
    repo_root: Optional[Path] = None,
    source_contents: Optional[Dict[str, str]] = None,
) -> List[str]:
    """Check that all newly introduced public symbols have non-test call sites.

    Returns a list of violation strings (empty if all symbols are reachable).
    """
    if not isinstance(files, (list, tuple)) or not files:
        return []

    violations: List[str] = []
    root = repo_root or Path(__file__).resolve().parents[1]

    # Collect source files to check
    changed_sources: List[Tuple[str, str]] = []
    for f in files:
        if not isinstance(f, dict):
            continue
        path = f.get("path", "")
        if not _is_source_python_file(path):
            continue
        content = f.get("content")
        if content is None and source_contents and path in source_contents:
            content = source_contents[path]
        if content is None:
            full_p = root / path
            if full_p.exists():
                try:
                    content = full_p.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    pass
        if content:
            changed_sources.append((path, content))

    if not changed_sources:
        return []

    # If source_contents is explicitly provided (hermetic/test mode)
    if source_contents is not None:
        for path, content in changed_sources:
            symbols = extract_top_level_symbols(content)
            for sym in symbols:
                refs = find_symbol_references(sym, path, source_contents)
                if not refs:
                    violations.append(
                        f"DEAD_CODE_UNREACHABLE: symbol '{sym}' defined in '{path}' "
                        f"has 0 production call sites outside its own module"
                    )
        return violations

    # Otherwise scan repository via git grep / filesystem
    for path, content in changed_sources:
        symbols = extract_top_level_symbols(content)
        for sym in symbols:
            # Try git grep first
            pattern = rf"\b{sym}\b"
            found = False
            try:
                res = subprocess.run(
                    ["git", "grep", "-l", "-E", pattern, "--", "*.py"],
                    cwd=str(root),
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if res.returncode == 0:
                    matching_files = [
                        line.strip()
                        for line in res.stdout.splitlines()
                        if line.strip()
                        and not _is_test_file(line.strip())
                        and line.strip() != path
                    ]
                    if matching_files:
                        found = True
            except Exception:
                pass

            if not found:
                violations.append(
                    f"DEAD_CODE_UNREACHABLE: symbol '{sym}' defined in '{path}' "
                    f"has 0 production call sites outside its own module"
                )

    return violations


def main(argv: List[str]) -> int:
    if "--help" in argv or "-h" in argv:
        print("usage: evolution_reachability_gate.py [--pr N] [--files FILES_JSON]")
        return 2

    # CLI mode
    root = Path(__file__).resolve().parents[1]
    files_arg = None
    if "--files" in argv:
        idx = argv.index("--files")
        if idx + 1 < len(argv):
            files_arg = argv[idx + 1]

    if files_arg:
        try:
            files = json.loads(files_arg)
        except json.JSONDecodeError:
            print("[reachability-gate] Invalid JSON in --files", file=sys.stderr)
            return 1
    else:
        # Fallback: check uncommitted or HEAD changes
        files = []

    violations = check_reachability(files, repo_root=root)
    if violations:
        print("[reachability-gate] BLOCKED by dead-code reachability policy:")
        for v in violations:
            print(f"  • {v}")
        return 1

    print("[reachability-gate] Reachability policy OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
