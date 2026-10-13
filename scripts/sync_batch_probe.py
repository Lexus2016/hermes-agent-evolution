#!/usr/bin/env python3
"""Batch-probe: per-tag, per-subsystem conflict sizing (#3457).

For each --tag, runs a throwaway `git merge --no-commit` in an isolated
`git worktree add --detach`, counts conflicted files total and per
subsystem bucket, aborts, asserts a clean tree. Emits JSON to stdout.
NEVER pushes, NEVER leaves a dirty tree. Minimal slice: probe core
only; the markdown owner-brief is deferred to a follow-up slice.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

BUCKETS: tuple[str, ...] = (".github", "agent", "cron", "hermes_cli", "tools")


@dataclass
class TagProbe:
    tag: str
    buckets: dict[str, int] = field(default_factory=dict)
    total: int = 0
    error: str | None = None


def _bucket_for(path: str) -> str:
    return next(
        (b for b in BUCKETS if path.startswith(b + "/") or path == b),
        "other",
    )


def _run(cmd: list[str], cwd: str) -> subprocess.CompletedProcess:
    # encoding/errors guard the blocking check_windows_footguns lint
    return subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _probe_tag(source: str, tag: str) -> TagProbe:
    probe = TagProbe(tag=tag, buckets={b: 0 for b in (*BUCKETS, "other")})
    src_sha = _run(["git", "rev-parse", "--verify", source], ".").stdout.strip()
    if not src_sha:
        probe.error = f"source ref {source!r} not resolvable"
        return probe
    with tempfile.TemporaryDirectory(prefix="sync_batch_probe_") as tmp:
        wt = Path(tmp) / "wt"
        _run(["git", "fetch", "--tags", "origin", tag], ".")
        add = _run(["git", "worktree", "add", "--detach", str(wt), src_sha], ".")
        if add.returncode != 0:
            probe.error = f"worktree add failed: {add.stderr.strip()[:120]}"
            return probe
        try:
            if _run(["git", "rev-parse", "--verify", tag], str(wt)).returncode != 0:
                probe.error = f"tag {tag!r} not resolvable"
                return probe
            _run(["git", "merge", "--no-ff", "--no-commit", tag], str(wt))
            ls = _run(["git", "diff", "--name-only", "--diff-filter=U"], str(wt))
            for line in ls.stdout.splitlines():
                line = line.strip()
                if line:
                    b = _bucket_for(line)
                    probe.buckets[b] = probe.buckets.get(b, 0) + 1
                    probe.total += 1
            _run(["git", "merge", "--abort"], str(wt))
            status = _run(["git", "status", "--porcelain"], str(wt))
            if status.stdout.strip():
                probe.error = f"worktree not clean: {status.stdout.strip()[:120]}"
                probe.total = 0
                probe.buckets = {b: 0 for b in (*BUCKETS, "other")}
        finally:
            _run(["git", "worktree", "remove", "--force", str(wt)], ".")
    return probe


def main() -> int:
    ap = argparse.ArgumentParser(description="Per-tag conflict sizing probe.")
    ap.add_argument("--source", default="origin/main", help="ref to probe against")
    ap.add_argument("--tags", nargs="+", required=True, help="upstream tags to probe")
    args = ap.parse_args()
    probes = [_probe_tag(args.source, t) for t in args.tags]
    print(json.dumps([vars(p) for p in probes], indent=2, sort_keys=True))
    return 0 if not any(p.error for p in probes) else 2


if __name__ == "__main__":
    sys.exit(main())
