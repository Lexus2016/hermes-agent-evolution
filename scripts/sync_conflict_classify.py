#!/usr/bin/env python3
"""Read-only conflict classification from three committed trees (#3456).

Called through sync_classify.py --base REF --fork REF --upstream REF
--paths-file PATH --out-dir analysis. Input is a NUL-delimited path list from
sync_batch_probe.py or an unquoted newline-delimited list from an aborted merge.
No working-tree state or network access is required.

Fork unchanged from base: upstream-wins; upstream unchanged: fork-keeps;
both changed: manual (exit 1). Missing entries preserve addition/deletion
intent. Tree object IDs compare binary bytes and file modes without decoding.
JSON records immutable refs and each side's mode/type/object ID as evidence;
markdown summarizes verdicts and subsystem counts. No resolution is performed.
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
    base_entry: str | None
    fork_entry: str | None
    upstream_entry: str | None


def _resolve(ref: str) -> str:
    return subprocess.run(
        ["git", "rev-parse", "--verify", "--end-of-options", ref + "^{commit}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    ).stdout.strip()


def _tree(ref: str) -> dict[str, str]:
    # Object IDs compare binary bytes without lossy decoding; mode also matters.
    output = subprocess.run(
        ["git", "ls-tree", "--full-tree", "-r", "-z", ref],
        capture_output=True,
        check=True,
    ).stdout
    entries = {}
    for record in output.split(b"\0"):
        if record:
            entry, path = record.split(b"\t", 1)
            entries[path.decode("utf-8", "surrogateescape")] = entry.decode("ascii")
    return entries


def _bucket(path: str) -> str:
    for prefix in BUCKETS:
        if path.startswith(prefix + "/") or path == prefix:
            return prefix
    return "other"


def _verdict_for(base, fork, upstream) -> str:
    """Assign a verdict from the three side-blobs (None == path absent).

    Compare each side vs base. Keep the changed side when the other side is
    unchanged — so a fork deletion (fork=None) where
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
    trees = [_tree(_resolve(ref)) for ref in (base, fork, upstream)]
    out = []
    for p in sorted(set(paths)):
        entries = [tree.get(p) for tree in trees]
        if all(entry is None for entry in entries):
            raise ValueError(f"path absent from all three trees: {p!r}")
        out.append(
            Verdict(
                p,
                _verdict_for(*entries),
                _bucket(p),
                *(entry is not None for entry in entries),
                *entries,
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
    text = p.read_bytes().decode("utf-8", "surrogateescape")
    return [
        line
        for line in (text.split("\0") if "\0" in text else text.splitlines())
        if line
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
    if not paths and not args.paths_file:
        raise SystemExit("no paths provided (use --path or --paths-file)")

    tag = args.tag or args.upstream
    refs = {k: _resolve(getattr(args, k)) for k in ("base", "fork", "upstream")}
    verdicts = classify(paths, **refs)
    summary = _summary(verdicts)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    filename = tag.replace("/", "_").replace("\\", "_")
    json_path = out_dir / f"sync-conflicts-{filename}.json"
    md_path = out_dir / f"sync-conflicts-{filename}.md"
    json_path.write_text(
        json.dumps(
            {
                "tag": tag,
                **refs,
                "summary": summary,
                "verdicts": [asdict(v) for v in verdicts],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    md_path.write_text(
        _render_markdown(tag, summary, verdicts),
        encoding="utf-8",
        errors="backslashreplace",
    )

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
