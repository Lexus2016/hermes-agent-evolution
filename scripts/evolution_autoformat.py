#!/usr/bin/env python3
"""Auto-format changed Python files before CI — prevents format-fail merge blocks (#1540).

CI's ``ruff check .`` (blocking in ``lint.yml``) rejects unformatted code. When
the implementation agent forgets to run ``ruff check --fix`` + ``ruff format``,
the PR sits red until a human formats it. This script runs both on ONLY the
changed files (vs ``base``, default ``origin/main``), called by the
implementation skill before commit and the integration skill as format-then-retry.

Usage::

    python scripts/evolution_autoformat.py                    # format changed files
    python scripts/evolution_autoformat.py --base origin/main # diff vs custom ref
    python scripts/evolution_autoformat.py --dry-run          # report only
    python scripts/evolution_autoformat.py --files a.py,b.py  # explicit file list

Exit 0 = success (or dry-run), 1 = formatting error.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Sequence, Tuple


def _run(cmd: List[str], cwd: Optional[str] = None) -> Tuple[int, str, str]:
    p = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
    return p.returncode, p.stdout, p.stderr


def get_changed_files(
    base: str = "origin/main", cwd: Optional[str] = None
) -> List[str]:
    """Return Python files changed vs ``base`` that still exist on disk."""
    code, out, _ = _run(["git", "diff", "--name-only", base], cwd=cwd)
    if code != 0:
        return []
    root = Path(cwd) if cwd else Path.cwd()
    return [
        line.strip()
        for line in out.strip().splitlines()
        if line.strip().endswith(".py") and (root / line.strip()).exists()
    ]


def format_files(
    files: Sequence[str], *, dry_run: bool = False, runner=subprocess.run
) -> Tuple[bool, List[str]]:
    """Run ``ruff check --fix`` + ``ruff format`` on ``files``.

    Returns ``(success, messages)``. Dry-run uses ``--check`` variants.
    """
    if not files:
        return True, ["no Python files to format"]

    messages: List[str] = []
    # Step 1: ruff check --fix (lint auto-fix; --exit-zero in dry-run)
    check_cmd = ["ruff", "check", "--exit-zero" if dry_run else "--fix", *files]
    p = runner(check_cmd, capture_output=True, text=True)
    if not dry_run and p.returncode != 0:
        messages.append(f"ruff check: {p.stdout.strip() or p.stderr.strip()}")
    elif dry_run and p.stdout.strip():
        messages.append(f"ruff check (dry-run): {p.stdout.strip()}")

    # Step 2: ruff format (--check in dry-run)
    fmt_cmd = ["ruff", "format", *(["--check"] if dry_run else []), *files]
    p2 = runner(fmt_cmd, capture_output=True, text=True)
    if not dry_run and p2.returncode != 0:
        messages.append(f"ruff format FAILED: {p2.stderr.strip()}")
        return False, messages
    if dry_run and p2.returncode != 0:
        messages.append(f"files need formatting:\n{p2.stdout.strip()}")
    elif dry_run:
        messages.append("all files already formatted")

    return True, messages


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Auto-format changed Python files before CI"
    )
    parser.add_argument("--base", default="origin/main", help="Git ref to diff against")
    parser.add_argument("--files", help="Comma-separated file list (skips git diff)")
    parser.add_argument(
        "--dry-run", action="store_true", help="Report without modifying"
    )
    args = parser.parse_args(argv)

    if args.files:
        files = [f.strip() for f in args.files.split(",") if f.strip()]
    else:
        files = get_changed_files(args.base)
        if not files:
            print("[autoformat] no changed Python files — nothing to format")
            return 0

    print(
        f"[autoformat] {'checking' if args.dry_run else 'formatting'} {len(files)} file(s)"
    )
    success, messages = format_files(files, dry_run=args.dry_run)
    for msg in messages:
        print(f"  {msg}")
    if not success:
        print("[autoformat] FORMATTING FAILED")
        return 1

    action = "would be formatted" if args.dry_run else "formatted successfully"
    print(f"[autoformat] {len(files)} file(s) {action}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
