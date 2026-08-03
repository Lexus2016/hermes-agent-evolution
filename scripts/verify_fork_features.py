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


def _git(*args: str) -> str:
    """Run a git command, returning stdout (empty string on failure)."""
    try:
        out = subprocess.run(
            ["git", *args], capture_output=True, check=True, timeout=300
        )
        return out.stdout.decode("utf-8", errors="replace")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return ""


def _markers_in(ref: str) -> set[str]:
    """Issue markers (#1234) appearing anywhere under CODE_PATHS at *ref*."""
    raw = subprocess.run(
        ["git", "grep", "-ohE", "#[0-9]{3,5}", ref, "--", *CODE_PATHS],
        capture_output=True,
        timeout=600,
    ).stdout
    return {m.decode() for m in _MARKER_RE.findall(raw)}


def _files_in(ref: str) -> set[str]:
    return {ln for ln in _git("ls-tree", "-r", "--name-only", ref).splitlines() if ln}


def _worktree_markers() -> set[str]:
    raw = subprocess.run(
        ["git", "grep", "-ohE", "#[0-9]{3,5}", "--", *CODE_PATHS],
        capture_output=True,
        timeout=600,
    ).stdout
    return {m.decode() for m in _MARKER_RE.findall(raw)}


def cmd_snapshot(args: argparse.Namespace) -> int:
    fork_markers = _markers_in(args.fork)
    upstream_markers = _markers_in(args.upstream)
    owned_markers = sorted(fork_markers - upstream_markers)

    fork_files = _files_in(args.fork)
    upstream_files = _files_in(args.upstream)
    owned_files = sorted(
        f
        for f in (fork_files - upstream_files)
        if any(f.startswith(p) for p in CODE_PATHS)
    )

    payload = {
        "fork_ref": args.fork,
        "upstream_ref": args.upstream,
        "fork_head": _git("rev-parse", args.fork).strip(),
        "upstream_head": _git("rev-parse", args.upstream).strip(),
        "owned_markers": owned_markers,
        "owned_files": owned_files,
    }
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(
        f"snapshot: {len(owned_markers)} fork-owned markers, "
        f"{len(owned_files)} fork-only code files -> {args.output}"
    )
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
    expected_markers = set(baseline["owned_markers"])
    expected_files = set(baseline["owned_files"])

    present_markers = _worktree_markers()
    missing_markers = sorted(expected_markers - present_markers)
    missing_files = sorted(f for f in expected_files if not Path(f).exists())

    if not missing_markers and not missing_files:
        print(
            f"OK — all {len(expected_markers)} fork-owned markers and "
            f"{len(expected_files)} fork-only files are present."
        )
        return 0

    if missing_files:
        print(f"\nMISSING FORK-ONLY FILES ({len(missing_files)}):", file=sys.stderr)
        for f in missing_files:
            print(f"  {f}", file=sys.stderr)

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
    snap.add_argument("-o", "--output", default=".evolution/fork-baseline.json")
    snap.set_defaults(func=cmd_snapshot)

    chk = sub.add_parser("check", help="verify the working tree still has it all")
    chk.add_argument("--baseline", default=".evolution/fork-baseline.json")
    chk.set_defaults(func=cmd_check)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
