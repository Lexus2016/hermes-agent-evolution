#!/usr/bin/env python3
"""Offline, independent release probes against one pinned source; never pushes.

Each candidate is merged only in a detached temporary worktree, then aborted
and checked clean. These are NOT simulated sequential merges: re-probe after
landing each release. AUTONOMOUS sizes conflict risk only, not merge approval.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

BUCKETS = (".github", "agent", "cron", "hermes_cli", "tools")


def _run(cmd: list[str], cwd: str = ".", *, check: bool = False):
    return subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=check,
    )


def _resolve(ref: str) -> str:
    return _run(
        ["git", "rev-parse", "--verify", "--end-of-options", ref + "^{commit}"],
        check=True,
    ).stdout.strip()


def _bucket_for(path: str) -> str:
    return next((b for b in BUCKETS if path.startswith(b + "/") or path == b), "other")


def _core(path: str) -> bool:
    # Conservatively include the entire CLI and persistence facade/siblings.
    return (
        path.startswith(("agent/", "hermes_cli/", "hermes_state", "cron/"))
        or path == "run_agent.py"
    )


@dataclass
class TagProbe:
    tag: str
    source_sha: str = ""
    tag_sha: str = ""
    buckets: dict[str, int] = field(
        default_factory=lambda: {b: 0 for b in (*BUCKETS, "other")}
    )
    paths: list[str] = field(default_factory=list)
    total: int = 0
    core: int = 0
    behind: int = 0
    error: str | None = None

    @property
    def verdict(self) -> str:
        return (
            "OWNER-SESSION"
            if self.error or self.core or self.total > 10
            else "AUTONOMOUS"
        )


def _probe_tag(source: str, tag: str) -> TagProbe:
    probe = TagProbe(tag)
    try:
        probe.source_sha, probe.tag_sha = _resolve(source), _resolve(tag)
        probe.behind = int(
            _run(
                ["git", "rev-list", "--count", f"{probe.source_sha}..{probe.tag_sha}"],
                check=True,
            ).stdout
        )
    except subprocess.CalledProcessError as exc:
        probe.error = exc.stderr.strip()
        return probe
    with tempfile.TemporaryDirectory(prefix="sync_batch_probe_") as tmp:
        wt = str(Path(tmp) / "wt")
        add = _run([
            "git",
            "-c",
            "core.hooksPath=" + tmp,
            "worktree",
            "add",
            "--detach",
            wt,
            probe.source_sha,
        ])
        if add.returncode:
            probe.error = add.stderr.strip()
            return probe
        try:
            # Disable hooks/rerere so a probe neither executes hooks nor trains or
            # applies remembered resolutions. No fetch: refs must already exist.
            merge = _run(
                [
                    "git",
                    "-c",
                    "core.hooksPath=" + tmp,
                    "-c",
                    "rerere.enabled=false",
                    "merge",
                    "--no-ff",
                    "--no-commit",
                    probe.tag_sha,
                ],
                wt,
            )
            raw_paths = subprocess.run(
                ["git", "diff", "--name-only", "--diff-filter=U", "-z"],
                cwd=wt,
                capture_output=True,
                check=True,
            ).stdout
            probe.paths = raw_paths.decode("utf-8", "surrogateescape").split("\0")
            probe.paths = [p for p in probe.paths if p]
            if merge.returncode and not (merge.returncode == 1 and probe.paths):
                probe.error = merge.stderr.strip() or merge.stdout.strip()
            for path in probe.paths:
                probe.buckets[_bucket_for(path)] += 1
            probe.total, probe.core = len(probe.paths), sum(map(_core, probe.paths))
        except subprocess.CalledProcessError as exc:
            probe.error = str(exc)
        finally:
            try:
                if (
                    _run(
                        ["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"], wt
                    ).returncode
                    == 0
                ):
                    _run(["git", "merge", "--abort"], wt, check=True)
                status = _run(["git", "status", "--porcelain"], wt, check=True)
                if status.stdout:
                    raise RuntimeError(f"probe worktree not clean: {status.stdout}")
            finally:
                _run(["git", "worktree", "remove", "--force", wt], check=True)
    return probe


def _markdown(probes: list[TagProbe]) -> str:
    columns = ("tag", *BUCKETS, "other", "total", "core", "behind", "verdict")
    lines = [
        "# Independent upstream conflict probes",
        "",
        "All tags use the same source, not a sequential merge simulation.",
        "AUTONOMOUS means ≤10 non-core conflicts only; other sync gates still apply.",
        "",
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join(["---"] * len(columns)) + " |",
    ]
    for p in probes:
        cells = [
            p.tag,
            *(p.buckets[b] for b in (*BUCKETS, "other")),
            p.total,
            p.core,
            p.behind,
            p.verdict,
        ]
        lines.append(
            "| " + " | ".join(str(c).replace("|", "\\|") for c in cells) + " |"
        )
    for p in probes:
        lines.extend(["", f"{p.tag}: source `{p.source_sha}`, target `{p.tag_sha}`."])
        if p.error:
            lines.append(f"Probe error (not a zero-conflict success): {p.error}")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", default="origin/main")
    ap.add_argument("--tags", nargs="+", required=True)
    ap.add_argument(
        "--out-dir",
        type=Path,
        help="write JSON, markdown and NUL-delimited conflict lists",
    )
    args = ap.parse_args()
    try:
        source = _resolve(args.source)  # pin once for the entire batch
    except subprocess.CalledProcessError as exc:
        print(exc.stderr.strip(), file=sys.stderr)
        return 2
    probes = [_probe_tag(source, t) for t in args.tags]
    payload = (
        json.dumps([{**vars(p), "verdict": p.verdict} for p in probes], indent=2) + "\n"
    )
    if args.out_dir:
        args.out_dir.mkdir(parents=True, exist_ok=True)
        (args.out_dir / "sync-batch-probe.json").write_text(payload, encoding="utf-8")
        (args.out_dir / "sync-batch-probe.md").write_text(
            _markdown(probes), encoding="utf-8", errors="backslashreplace"
        )
        for p in probes:
            name = p.tag.replace("/", "_").replace("\\", "_")
            (args.out_dir / f"sync-paths-{name}.nul").write_bytes(
                ("\0".join(p.paths) + ("\0" if p.paths else "")).encode(
                    "utf-8", "surrogateescape"
                )
            )
    print(payload, end="")
    return 2 if any(p.error for p in probes) else 0


if __name__ == "__main__":
    sys.exit(main())
