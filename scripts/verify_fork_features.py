#!/usr/bin/env python3
"""Verify no fork feature was silently dropped by an upstream sync.

The fork's features are additive edits *inside* otherwise-upstream files, so a
plain diff against upstream is mostly noise. This checks the two things that
are actually machine-checkable:

1. **Fork-only issue markers.** Every fork feature carries an issue reference
   (``#1234``) in the code that implements it. Markers that appear in the fork
   tree but not in the upstream tree are fork-owned; every one of them must
   survive a sync.
2. **Fork-only files.** Files that exist in the fork and not upstream must not
   be deleted by a merge.

Both baselines are computed from git refs rather than a checked-in snapshot, so
the check cannot silently drift out of date.

Usage::

    # before syncing — record what the fork owns
    python3 scripts/verify_fork_features.py snapshot \\
        --fork origin/main --upstream upstream/main -o .evolution/fork-baseline.json

    # after each merge step — confirm nothing vanished
    python3 scripts/verify_fork_features.py check \\
        --baseline .evolution/fork-baseline.json

Exit code is 1 when anything is missing, so it can gate a sync branch in CI.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from pathlib import Path

# Directories whose fork edits are feature-bearing. Tests and docs are excluded:
# they are verified by actually running them, and their churn would swamp the
# signal here.
CODE_PATHS = [
    "agent/",
    "cron/",
    "tools/",
    "hermes_cli/",
    "gateway/",
    "plugins/",
    "run_agent.py",
    "cli.py",
    "hermes_state.py",
    "model_tools.py",
    "toolsets.py",
]

_MARKER_RE = re.compile(rb"#[0-9]{3,5}")

# A marker present at the MERGE-BASE predates the fork, so it is upstream's no
# matter what — this is the exact test, and it is why ``snapshot`` takes a
# --base. The numeric ceiling below is only a coarse fallback for when no base
# ref is supplied: upstream issue numbers are five digits and climbing
# (#68217), this fork's are three or four (#102 .. #1640).
#
# Both filters exist because "in our tree, not in upstream's HEAD" is NOT
# sufficient on its own: upstream routinely rewrites a comment after our
# merge-base, which makes their own marker look fork-only and reports a
# feature loss that never happened.
MAX_FORK_ISSUE = 9999


def _git(*args: str) -> str:
    """Run a git command, returning stdout (empty string on failure)."""
    try:
        out = subprocess.run(
            ["git", *args], capture_output=True, check=True, timeout=300
        )
        return out.stdout.decode("utf-8", errors="replace")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return ""


def _fork_range(markers) -> set[str]:
    """Keep only markers that could plausibly be this fork's own issues."""
    return {m for m in markers if int(m.lstrip("#")) <= MAX_FORK_ISSUE}


def _markers_in(ref: str) -> set[str]:
    """Issue markers (#1234) appearing anywhere under CODE_PATHS at *ref*."""
    raw = subprocess.run(
        ["git", "grep", "-ohE", "#[0-9]{3,5}", ref, "--", *CODE_PATHS],
        capture_output=True,
        timeout=600,
    ).stdout
    return _fork_range(m.decode() for m in _MARKER_RE.findall(raw))


def _files_in(ref: str) -> set[str]:
    return {ln for ln in _git("ls-tree", "-r", "--name-only", ref).splitlines() if ln}


def _worktree_markers() -> set[str]:
    raw = subprocess.run(
        ["git", "grep", "-ohE", "#[0-9]{3,5}", "--", *CODE_PATHS],
        capture_output=True,
        timeout=600,
    ).stdout
    return _fork_range(m.decode() for m in _MARKER_RE.findall(raw))


def _symbols_in(ref: str | None) -> dict[str, str]:
    """Top-level def/class/assignment names -> the first path defining them.

    Markers and fork-only files between them miss the most common way a sync
    loses fork work: a function the fork added to a file upstream also owns.
    The merge takes upstream's side, the file still exists, no marker moves,
    and the feature is simply gone.  Names are tracked tree-wide rather than
    per-file so upstream's frequent module extractions (config.py ->
    config_defaults.py) do not read as deletions.
    """
    if ref:
        paths = _git("ls-tree", "-r", "--name-only", ref).splitlines()
    else:
        paths = _git("ls-files").splitlines()

    out: dict[str, str] = {}
    for path in paths:
        if not path.endswith(".py") or path.startswith("tests/"):
            continue
        if ref:
            blob = subprocess.run(
                ["git", "show", f"{ref}:{path}"], capture_output=True, timeout=60
            )
            if blob.returncode != 0:
                continue
            source = blob.stdout.decode("utf-8", "replace")
        else:
            try:
                source = Path(path).read_text(encoding="utf-8-sig", errors="replace")
            except OSError:
                continue
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        for node in tree.body:
            names: list[str] = []
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names = [node.name]
            elif isinstance(node, ast.Assign):
                names = [
                    t.id
                    for t in node.targets
                    if isinstance(t, ast.Name) and not t.id.startswith("__")
                ]
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                # Annotated module-level assignments (``_x: float = 0.0``) define
                # the same name a plain assignment would; skipping them makes a
                # side that annotates look like it lacks the symbol (false
                # MISSING during sync checks).
                if not node.target.id.startswith("__"):
                    names = [node.target.id]
            for name in names:
                out.setdefault(name, path)
    return out


def cmd_snapshot(args: argparse.Namespace) -> int:
    fork_markers = _markers_in(args.fork)
    upstream_markers = _markers_in(args.upstream)
    base_markers = _markers_in(args.base) if args.base else set()
    owned_markers = sorted(fork_markers - upstream_markers - base_markers)

    fork_files = _files_in(args.fork)
    upstream_files = _files_in(args.upstream)
    base_files = _files_in(args.base) if args.base else set()
    owned_files = sorted(
        f
        for f in (fork_files - upstream_files - base_files)
        if any(f.startswith(p) for p in CODE_PATHS)
    )

    # Subtracting the base matters as much here as it does for markers: a name
    # the fork and the merge-base both carry, and upstream has since deleted,
    # is an upstream decision to honour — not fork work to restore.
    fork_symbols = _symbols_in(args.fork)
    upstream_symbols = _symbols_in(args.upstream)
    base_symbols = _symbols_in(args.base) if args.base else {}
    owned_symbols = sorted(
        set(fork_symbols) - set(upstream_symbols) - set(base_symbols)
    )

    payload = {
        "fork_ref": args.fork,
        "upstream_ref": args.upstream,
        "base_ref": args.base,
        "fork_head": _git("rev-parse", args.fork).strip(),
        "upstream_head": _git("rev-parse", args.upstream).strip(),
        "owned_markers": owned_markers,
        "owned_files": owned_files,
        "owned_symbols": owned_symbols,
    }
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(
        f"snapshot: {len(owned_markers)} fork-owned markers, "
        f"{len(owned_files)} fork-only code files, "
        f"{len(owned_symbols)} fork-only symbols -> {args.output}"
    )
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8-sig"))
    expected_markers = set(baseline["owned_markers"])
    expected_files = set(baseline["owned_files"])
    expected_symbols = set(baseline.get("owned_symbols", ()))

    present_markers = _worktree_markers()
    missing_markers = sorted(expected_markers - present_markers)
    missing_files = sorted(f for f in expected_files if not Path(f).exists())
    missing_symbols = sorted(expected_symbols - set(_symbols_in(None)))

    if not missing_markers and not missing_files and not missing_symbols:
        print(
            f"OK — all {len(expected_markers)} fork-owned markers, "
            f"{len(expected_files)} fork-only files and "
            f"{len(expected_symbols)} fork-only symbols are present."
        )
        return 0

    if missing_files:
        print(f"\nMISSING FORK-ONLY FILES ({len(missing_files)}):", file=sys.stderr)
        for f in missing_files:
            print(f"  {f}", file=sys.stderr)

    if missing_symbols:
        print(f"\nMISSING FORK-ONLY SYMBOLS ({len(missing_symbols)}):", file=sys.stderr)
        for s in missing_symbols:
            print(f"  {s}  (was in {baseline['fork_ref']})", file=sys.stderr)
        print(
            "\nEach of these is a definition the fork added to a file upstream "
            "also owns,\nand the integration took upstream's side. Restore it, "
            "or drop it from the\nbaseline with the reason recorded in the sync "
            "notes.",
            file=sys.stderr,
        )

    if missing_markers:
        print(f"\nMISSING FORK MARKERS ({len(missing_markers)}):", file=sys.stderr)
        for m in missing_markers:
            print(f"  {m}", file=sys.stderr)
        print(
            "\nA missing marker means the code implementing that issue is gone from "
            "the tree.\nFor each one, either restore the feature or — if upstream "
            "now implements it natively —\nrecord that in the sync notes and drop "
            "the marker from the baseline deliberately.",
            file=sys.stderr,
        )
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    snap = sub.add_parser("snapshot", help="record what the fork owns")
    snap.add_argument("--fork", default="origin/main")
    snap.add_argument("--upstream", default="upstream/main")
    snap.add_argument(
        "--base",
        default=None,
        help=(
            "merge-base ref. Anything present here predates the fork, so it is "
            "upstream's — the exact ownership test. Strongly recommended."
        ),
    )
    snap.add_argument("-o", "--output", default=".evolution/fork-baseline.json")
    snap.set_defaults(func=cmd_snapshot)

    chk = sub.add_parser("check", help="verify the working tree still has it all")
    chk.add_argument("--baseline", default=".evolution/fork-baseline.json")
    chk.set_defaults(func=cmd_check)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
