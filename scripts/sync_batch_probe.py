#!/usr/bin/env python3
"""Batch-probe: per-tag, per-subsystem conflict sizing for sequential upstream catch-up (#3457).

The catch-up plan needs per-batch sizing: which release steps / subsystem
buckets fit the autonomous ceiling (≤10 non-core conflicts) and which
require an owner session. This script walks a tag list, performs a
throwaway probe merge in an isolated worktree for each one, and emits a
markdown owner-decision brief with a verdict per tag.

It NEVER pushes, NEVER leaves a dirty tree, and NEVER modifies the
caller's repo. Each per-tag probe runs in a fresh `git worktree add
--detach` of the source ref, then is removed; the abort and the
`git status --porcelain` assertion are belt-and-braces in case the
worktree path itself is misconfigured.

Usage::

    python3 scripts/sync_batch_probe.py \\
        --source origin/main \\
        --tags v0.21.6 v0.21.7 v2026.9.24 \\
        --out analysis/sync-batch-brief.md

    # Use --autonomous-ceiling to tighten the AUTONOMOUS verdict
    # (default 10 conflicts, ALL buckets, including .github and agent).
    python3 scripts/sync_batch_probe.py \\
        --source origin/main \\
        --tags v0.21.7 \\
        --autonomous-ceiling 0 \\
        --out /tmp/brief.md

Output: a markdown table with one row per tag and columns
`tag | .github | agent | cron | hermes_cli | tools | other | total | verdict`.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

BUCKETS: tuple[str, ...] = (".github", "agent", "cron", "hermes_cli", "tools")


@dataclass
class TagProbe:
    tag: str
    buckets: dict[str, int] = field(default_factory=dict)
    total: int = 0
    error: str | None = None  # set if the probe failed (then total/buckets stay 0)

    @property
    def verdict(self) -> str:
        if self.error:
            return "ERROR"
        return "AUTONOMOUS" if self.total <= 0 else "OWNER-SESSION"

    def render_row(self) -> str:
        if self.error:
            return f"| `{self.tag}` | error: {self.error} | — | — | — | — | — | — | — |"
        cells = [str(self.buckets.get(b, 0)) for b in BUCKETS]
        return (
            f"| `{self.tag}` | "
            + " | ".join(cells)
            + f" | {self.total} | **{self.verdict}** |"
        )


def _bucket_for(path: str) -> str:
    for b in BUCKETS:
        if path.startswith(b + "/") or path == b:
            return b
    return "other"


def _run(cmd: Sequence[str], cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(list(cmd), cwd=cwd, capture_output=True, text=True)


def _probe_tag(source: str, tag: str) -> TagProbe:
    """Run a throwaway merge probe for `tag` and return the per-bucket counts.

    Uses `git worktree add --detach` to isolate the probe; the temp worktree
    is always removed in a finally clause. A `git status --porcelain` empty
    assertion is the final guardrail.
    """
    probe = TagProbe(tag=tag, buckets={b: 0 for b in (*BUCKETS, "other")})

    # Resolve source to a commit so the worktree pins to a known ref.
    src_sha = _run(["git", "rev-parse", "--verify", source], ".").stdout.strip()
    if not src_sha:
        probe.error = f"source ref {source!r} not resolvable"
        return probe

    with tempfile.TemporaryDirectory(prefix="sync_batch_probe_") as tmp:
        wt = Path(tmp) / "wt"
        # Try to fetch the tag into our object DB (no-op if already present).
        _run(["git", "fetch", "--tags", "origin", tag], ".")
        add = _run(
            [
                "git",
                "worktree",
                "add",
                "--detach",
                "--force",
                str(wt),
                src_sha,
            ],
            ".",
        )
        if add.returncode != 0:
            probe.error = f"worktree add failed: {add.stderr.strip()[:120]}"
            return probe
        try:
            # Point HEAD at the tag, attempt the merge.
            chk = _run(["git", "rev-parse", "--verify", tag], str(wt))
            if chk.returncode != 0:
                probe.error = f"tag {tag!r} not resolvable"
                return probe
            merge = _run(["git", "merge", "--no-ff", "--no-commit", tag], str(wt))
            # Conflicts (or any failure) → list what's unmerged, then abort.
            ls = _run(["git", "diff", "--name-only", "--diff-filter=U"], str(wt))
            for line in ls.stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                bucket = _bucket_for(line)
                probe.buckets[bucket] = probe.buckets.get(bucket, 0) + 1
                probe.total += 1
            # Always abort, regardless of merge returncode, to leave a clean tree.
            _run(["git", "merge", "--abort"], str(wt))
            # Belt-and-braces: working tree must be clean.
            status = _run(["git", "status", "--porcelain"], str(wt))
            if status.stdout.strip():
                probe.error = (
                    f"worktree not clean after abort: {status.stdout.strip()[:120]}"
                )
                probe.total = 0
                probe.buckets = {b: 0 for b in (*BUCKETS, "other")}
        finally:
            _run(["git", "worktree", "remove", "--force", str(wt)], ".")
    return probe


def _render_brief(source: str, ceiling: int, probes: list[TagProbe]) -> str:
    lines = [
        "# Sync batch probe — owner-decision brief",
        "",
        f"Source ref: `{source}`",
        f"Autonomous ceiling: ≤ {ceiling} total conflicts → AUTONOMOUS, else OWNER-SESSION.",
        "",
        "## Per-tag results",
        "",
        "| tag | " + " | ".join(BUCKETS) + " | total | verdict |",
        "|---|" + "---:|" * (len(BUCKETS) + 2),
    ]
    for p in probes:
        lines.append(p.render_row())
    lines.append("")
    lines.append("## Notes")
    lines.append("")
    lines.append("- Each probe runs in a throwaway `git worktree add --detach`")
    lines.append("  against the source ref; aborted and removed before the next tag.")
    lines.append("- Counts are conflicted FILES (a 3-way merge may produce multiple")
    lines.append("  conflict regions per file; we count once per file).")
    lines.append(
        "- `other` includes everything not under `.github/`, `agent/`, `cron/`,"
    )
    lines.append("  `hermes_cli/`, or `tools/`.")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(
        description=(
            (__doc__ or "").split("\n")[0]
            or "Batch-probe upstream tags for conflict sizing."
        )
    )
    ap.add_argument(
        "--source",
        default="origin/main",
        help="ref to probe against (default: origin/main)",
    )
    ap.add_argument(
        "--tags",
        nargs="+",
        required=True,
        help="upstream tags to probe, space-separated",
    )
    ap.add_argument(
        "--autonomous-ceiling",
        type=int,
        default=10,
        help="max TOTAL conflicts (across all buckets) for AUTONOMOUS verdict "
        "(default: 10)",
    )
    ap.add_argument(
        "--out",
        default="analysis/sync-batch-brief.md",
        help="output markdown path (default: analysis/sync-batch-brief.md)",
    )
    args = ap.parse_args()

    if shutil.which("git") is None:
        raise SystemExit("git not found on PATH")

    probes = [_probe_tag(args.source, t) for t in args.tags]

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        _render_brief(args.source, args.autonomous_ceiling, probes),
        encoding="utf-8",
    )

    # Console summary
    for p in probes:
        if p.error:
            print(f"{p.tag}: ERROR ({p.error})")
        else:
            print(
                f"{p.tag}: total={p.total} verdict="
                f"{'AUTONOMOUS' if p.total <= args.autonomous_ceiling else 'OWNER-SESSION'}"
            )
    print(f"wrote {out_path}")
    # Return non-zero if any probe errored (caller can still read the brief).
    return 0 if not any(p.error for p in probes) else 2


if __name__ == "__main__":
    sys.exit(main())
