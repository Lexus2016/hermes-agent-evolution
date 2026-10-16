#!/usr/bin/env python3
"""Classify a test that vanished during an upstream sync: whose work was it?

The obvious rule — "present at the merge-base, so upstream authored it, so
accepting upstream's deletion is safe" — has a failure mode that silently drops
fork work:

    the test exists at the merge-base, the FORK then modifies it, and upstream
    later deletes it.

Under the obvious rule that reads as a clean upstream deletion, and the fork's
modification goes with it. Catching this needs the base -> fork diff, not just
membership in the base. This script reports that diff for every test that is in
both the base and the fork but absent from the working tree, so each one is
decided on evidence rather than on a name lookup.

    python3 scripts/sync_classify.py --base v2026.7.20 --fork origin/main tests/

Verdicts:

  FORK-AUTHORED     absent from the base entirely — the fork wrote it, restore.
  FORK-MODIFIED     in the base, but the fork changed it — read the diff. A
                    ruff-format reflow carries no intent and follows upstream's
                    deletion; a changed assertion or a new comment does.
  UPSTREAM-ONLY     byte-identical to the base — upstream authored and deleted
                    it, accept the deletion.
"""

from __future__ import annotations

import argparse
import ast
import difflib
import subprocess
import sys
from pathlib import Path


def _tests(source: str) -> dict[str, str]:
    """Test-function name -> its source text, decorators included."""
    out: dict[str, str] = {}
    lines = source.split("\n")

    def walk(node: ast.AST) -> None:
        for child in node.body:  # type: ignore[attr-defined]
            if isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef)
            ) and child.name.startswith("test_"):
                start = min([d.lineno for d in child.decorator_list] + [child.lineno])
                out[child.name] = "\n".join(lines[start - 1 : child.end_lineno])
            elif isinstance(child, ast.ClassDef):
                walk(child)

    try:
        walk(ast.parse(source))
    except SyntaxError:
        pass
    return out


def _at(ref: str, path: str) -> dict[str, str]:
    blob = subprocess.run(
        ["git", "show", f"{ref}:{path}"], capture_output=True, text=True
    )
    return _tests(blob.stdout) if blob.returncode == 0 else {}


def _paths(fork: str, roots: list[str]) -> list[str]:
    listed = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", fork, "--", *roots],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    return [p for p in listed if p.endswith(".py")]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--base", default="v2026.7.20", help="merge-base ref")
    ap.add_argument("--fork", default="origin/main", help="fork ref")
    ap.add_argument("--show-diff", action="store_true", help="print each FORK-MODIFIED diff")
    ap.add_argument("roots", nargs="*", default=["tests/"])
    args = ap.parse_args()

    counts = {"FORK-AUTHORED": 0, "FORK-MODIFIED": 0, "UPSTREAM-ONLY": 0}
    for path in _paths(args.fork, args.roots):
        base = _at(args.base, path)
        fork = _at(args.fork, path)
        try:
            current = set(_tests(Path(path).read_text(encoding="utf-8-sig")))
        except OSError:
            current = set()

        for name in sorted(fork):
            if name in current:
                continue
            if name not in base:
                verdict = "FORK-AUTHORED"
            elif base[name] != fork[name]:
                verdict = "FORK-MODIFIED"
            else:
                verdict = "UPSTREAM-ONLY"
            counts[verdict] += 1
            if verdict == "UPSTREAM-ONLY":
                continue
            print(f"{verdict:14} {path}::{name}")
            if verdict == "FORK-MODIFIED" and args.show_diff:
                diff = difflib.unified_diff(
                    base[name].split("\n"),
                    fork[name].split("\n"),
                    "base",
                    "fork",
                    lineterm="",
                )
                print("\n".join(f"    {line}" for line in diff))

    print(
        f"\nFORK-AUTHORED {counts['FORK-AUTHORED']} (restore) | "
        f"FORK-MODIFIED {counts['FORK-MODIFIED']} (read the diff) | "
        f"UPSTREAM-ONLY {counts['UPSTREAM-ONLY']} (deletion accepted)"
    )
    # Anything the fork touched and the tree lost needs a human verdict.
    return 1 if counts["FORK-AUTHORED"] or counts["FORK-MODIFIED"] else 0


if __name__ == "__main__":
    sys.exit(main())
