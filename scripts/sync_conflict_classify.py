#!/usr/bin/env python3
"""Classify a list of conflicted paths between fork and upstream (#3456).

The v0.21.7 catch-up produced 198 conflicted files across `.github/`, `agent/`,
`cron/`, `hermes_cli/`, `tools/`, and other paths. The owner needs a per-file
verdict — take upstream / keep fork / decide by hand — before any actual merge
is attempted. This script reads the blob at each side from the ref and assigns
a verdict WITHOUT performing a probe merge or touching the working tree.

Usage::

    # Path 1: feed it the conflicted paths from a probe merge that was aborted
    git diff --name-only --diff-filter=U > /tmp/conflicts.txt
    python3 scripts/sync_conflict_classify.py \
        --base v2026.9.24 --fork origin/main --upstream v0.21.7 \
        --paths-file /tmp/conflicts.txt --out-dir analysis/

    # Path 2: pass paths inline (one per arg)
    python3 scripts/sync_conflict_classify.py \
        --base v2026.9.24 --fork origin/main --upstream v0.21.7 \
        --path cron/legacy.py --path agent/gateway.py

Verdicts:

  upstream-wins  fork side is byte-identical to base — fork never touched it;
                 accepting upstream is safe.
  fork-keeps     upstream side is byte-identical to base — upstream never
                 changed it; the conflict is just a fork edit (or a fork
                 addition, or a fork deletion); keep fork.
  manual         both sides changed vs base, or both sides differ from each
                 other — needs an owner verdict (run a real 3-way merge and
                 inspect conflict markers).

A "side absent" case (the path doesn't exist in one of the three refs) is
classified by the OTHER side's relation to base — e.g. if the fork deleted it
and upstream kept it, the verdict is `fork-keeps` (the fork changed it). This
keeps the per-file logic one rule, not a 3x3 case grid.

Outputs (into --out-dir):

  sync-conflicts-<tag>.json    machine-readable: per-path verdicts + counts.
  sync-conflicts-<tag>.md      human summary with bucket breakdown.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

BUCKETS: tuple[str, ...] = (".github", "agent", "cron", "hermes_cli", "tools")


@dataclass(frozen=True)
class Verdict:
    path: str
    verdict: str  # upstream-wins | fork-keeps | manual
    bucket: str
    base_exists: bool
    fork_exists: bool
    upstream_exists: bool


def _blob(ref: str, path: str) -> bytes | None:
    """Return the file content at `ref:path` as bytes, or None if absent."""
    res = subprocess.run(["git", "show", f"{ref}:{path}"], capture_output=True)
    return res.stdout if res.returncode == 0 else None


def _bucket(path: str) -> str:
    for prefix in BUCKETS:
        if path.startswith(prefix + "/") or path == prefix:
            return prefix
    return "other"


def _verdict_for(base: bytes | None, fork: bytes | None, upstream: bytes | None) -> str:
    """Assign a verdict from the three side-blobs (None == path absent).

    One consistent rule, no 3x3 case grid: compare each side vs base. The side
    that did NOT change wins by default — so a fork deletion (fork=None) where
    upstream kept the file is ``fork-keeps`` (preserve the deletion), and a
    fork addition (fork=new, upstream=None) is also ``fork-keeps`` (preserve
    the new file). Only when BOTH sides changed do we hand off to ``manual``.
    """
    fork_changed = fork != base  # bytes-vs-None is "changed"
    upstream_changed = upstream != base
    if not fork_changed:
        return "upstream-wins"
    if not upstream_changed:
        return "fork-keeps"
    return "manual"


def classify(
    paths: Iterable[str], base: str, fork: str, upstream: str
) -> list[Verdict]:
    out: list[Verdict] = []
    for p in paths:
        v = _verdict_for(_blob(base, p), _blob(fork, p), _blob(upstream, p))
        out.append(
            Verdict(
                path=p,
                verdict=v,
                bucket=_bucket(p),
                base_exists=_blob(base, p) is not None,
                fork_exists=_blob(fork, p) is not None,
                upstream_exists=_blob(upstream, p) is not None,
            )
        )
    return out


def _summary(verdicts: list[Verdict]) -> dict:
    by_verdict: Counter[str] = Counter(v.verdict for v in verdicts)
    bucket_verdict: dict[str, Counter[str]] = {}
    for v in verdicts:
        bucket_verdict.setdefault(v.bucket, Counter())[v.verdict] += 1
    return {
        "total": len(verdicts),
        "by_verdict": dict(by_verdict),
        "by_bucket": {
            b: dict(bucket_verdict.get(b, Counter())) for b in (*BUCKETS, "other")
        },
    }


def _render_markdown(tag: str, summary: dict, verdicts: list[Verdict]) -> str:
    lines = [f"# Sync conflict classification — `{tag}`", ""]
    lines.append(f"Total paths: **{summary['total']}**")
    lines.append("")
    lines.append("## Verdicts")
    for k in ("upstream-wins", "fork-keeps", "manual"):
        lines.append(f"- {k}: {summary['by_verdict'].get(k, 0)}")
    lines.append("")
    lines.append("## Per-bucket breakdown")
    lines.append("")
    lines.append("| bucket | upstream-wins | fork-keeps | manual | total |")
    lines.append("|---|---:|---:|---:|---:|")
    for b in (*BUCKETS, "other"):
        row = summary["by_bucket"].get(b, {})
        total = sum(row.values())
        lines.append(
            f"| {b} | {row.get('upstream-wins', 0)} | "
            f"{row.get('fork-keeps', 0)} | {row.get('manual', 0)} | {total} |"
        )
    lines.append("")
    lines.append("## Per-path verdicts")
    lines.append("")
    lines.append("| path | verdict | bucket |")
    lines.append("|---|---|---|")
    for v in verdicts:
        lines.append(f"| `{v.path}` | {v.verdict} | {v.bucket} |")
    return "\n".join(lines) + "\n"


def _read_paths_file(path: str) -> list[str]:
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"paths file not found: {path}")
    return [
        line.strip()
        for line in p.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def main() -> int:
    desc = (__doc__ or "Classify conflicted paths between fork and upstream.").split(
        "\n"
    )[0]
    ap = argparse.ArgumentParser(description=desc)
    ap.add_argument("--base", required=True, help="merge-base ref")
    ap.add_argument("--fork", required=True, help="fork ref (ours)")
    ap.add_argument("--upstream", required=True, help="upstream ref (theirs)")
    ap.add_argument("--paths-file", help="file listing conflicted paths, one per line")
    ap.add_argument(
        "--path",
        action="append",
        default=[],
        help="conflicted path (repeatable; alternative to --paths-file)",
    )
    ap.add_argument(
        "--out-dir",
        default="analysis",
        help="directory for JSON+markdown report (default: analysis/)",
    )
    ap.add_argument(
        "--tag",
        default=None,
        help="tag used in the report filename (default: upstream ref)",
    )
    args = ap.parse_args()

    paths: list[str] = list(args.path)
    if args.paths_file:
        paths.extend(_read_paths_file(args.paths_file))
    if not paths:
        raise SystemExit("no paths provided (use --path or --paths-file)")

    tag = args.tag or args.upstream
    verdicts = classify(paths, args.base, args.fork, args.upstream)
    summary = _summary(verdicts)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"sync-conflicts-{tag}.json"
    md_path = out_dir / f"sync-conflicts-{tag}.md"
    json_path.write_text(
        json.dumps(
            {
                "tag": tag,
                "base": args.base,
                "fork": args.fork,
                "upstream": args.upstream,
                "summary": summary,
                "verdicts": [asdict(v) for v in verdicts],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    md_path.write_text(_render_markdown(tag, summary, verdicts), encoding="utf-8")

    print(
        f"wrote {json_path} and {md_path}: "
        f"upstream-wins {summary['by_verdict'].get('upstream-wins', 0)} | "
        f"fork-keeps {summary['by_verdict'].get('fork-keeps', 0)} | "
        f"manual {summary['by_verdict'].get('manual', 0)}"
    )
    # Manual paths always need an owner verdict.
    return 1 if summary["by_verdict"].get("manual", 0) else 0


if __name__ == "__main__":
    sys.exit(main())
