#!/usr/bin/env python3
"""Baseline + diff audit over the agent config/skill/hook trees (issue #89, slice A).

Scheduled as the ``no_agent`` job ``evolution-config-mutation-scan``
(``cron/evolution/config-mutation-scan.yaml``, 03:15 daily).

Background: Microsoft's ChainDrop analysis (2026-08-04) documents a
credential-stealing worm that persists via auto-append hooks in agent config
directories (``.claude/settings.json``, ``.vscode/tasks.json``, shell rc
files), surviving reinstall. The agent's own config, skills and hooks are
equally writable, and nothing currently detects an unexpected mutation — a
hook or directive appended to a config file or skill without a session having
made that change. This is slice A of the defense: a deterministic baseline +
diff primitive. Slice B adds attribution (which writes are session-owned),
slice C adds the auto-append hook scan and alerting.

What it does (all deterministic, no LLM, no MCP):

1. snapshots a hash/mtime record of the config-ish files under ``HERMES_HOME``
   (``~/.hermes`` by default) plus an optional ``--project-root`` — skipping
   known-volatile subtrees (cache, backups, cron job state, evolution data,
   lock files),
2. persists the snapshot to ``~/.hermes/evolution/config_baseline.json``
   (``--init`` writes it explicitly; a scan auto-initializes when the baseline
   is missing, so the first scheduled run bootstraps itself),
3. every run after diffs current vs baseline and writes a report listing
   added / removed / modified files to
   ``~/.hermes/evolution/config_mutation_scan.json``.

Report-only by design: mutations are surfaced, not auto-reverted (reverting
legit session edits would be destructive — attribution is slice B).
``--fail-on-changes`` makes the exit code actionable for a future alerting
wrapper.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone, UTC
from pathlib import Path
from typing import Any

# Volatile subtrees and file kinds that would otherwise turn every scan into
# noise: caches, backup mirrors, job state, the evolution pipeline's own data,
# lock/temp files. Deliberately name-based so it works across machines.
EXCLUDE_DIR_NAMES = {
    ".git",
    ".venv",
    "__pycache__",
    "node_modules",
    "cache",
    "backups",
    "checkpoints",
    "cron",
    "data",
    "logs",
    "document_cache",
    "audio_cache",
    "chrome-debug",
    ".curator_backups",
    "evolution",
    "ambient",
    "docker",
    "mcp_pins",
    ".snapshots",
}

EXCLUDE_FILE_SUFFIXES = {".lock", ".tmp", ".swp", ".pyc"}

# The persistence-vector surface: anything that can carry a hook or directive.
INCLUDE_EXTS = {
    ".yaml",
    ".yml",
    ".json",
    ".toml",
    ".conf",
    ".ini",
    ".cfg",
    ".md",
    ".txt",
    ".sh",
    ".py",
}

DEFAULT_REPORT = Path.home() / ".hermes" / "evolution" / "config_mutation_scan.json"
DEFAULT_BASELINE = Path.home() / ".hermes" / "evolution" / "config_baseline.json"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot_tree(root: Path) -> dict[str, dict[str, Any]]:
    """Snapshot one tree: ``{relpath: {"sha256", "size", "mtime_ns"}}``.

    Only files whose suffix is in ``INCLUDE_EXTS`` are recorded; subtrees whose
    name is in ``EXCLUDE_DIR_NAMES`` and files whose suffix is in
    ``EXCLUDE_FILE_SUFFIXES`` are skipped. Deterministic: paths are walked in
    sorted order.
    """
    snapshot: dict[str, dict[str, Any]] = {}
    if not root.is_dir():
        return snapshot
    for path in sorted(root.rglob("*")):
        if path.is_dir():
            continue
        if any(part in EXCLUDE_DIR_NAMES for part in path.parts):
            continue
        if path.suffix.lower() not in INCLUDE_EXTS:
            continue
        if path.suffix.lower() in EXCLUDE_FILE_SUFFIXES:
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        snapshot[str(path.relative_to(root))] = {
            "sha256": _sha256(path),
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
        }
    return snapshot


def snapshot_roots(roots: list[Path]) -> dict[str, dict[str, Any]]:
    """Snapshot multiple roots into one baseline, keyed ``<root.name>/<relpath>``.

    The root-name prefix keeps paths unambiguous when a project root shares
    file names with HERMES_HOME (e.g. both have ``config.yaml``).
    """
    merged: dict[str, dict[str, Any]] = {}
    for root in roots:
        for relpath, meta in snapshot_tree(root).items():
            merged[f"{root.name}/{relpath}"] = meta
    return merged


def detect_mutations(
    baseline: dict[str, dict[str, Any]],
    current: dict[str, dict[str, Any]],
) -> dict[str, list[Any]]:
    """Diff current against baseline.

    Returns ``{"added": [...], "removed": [...], "modified": [...]}``, each a
    sorted list of paths (modified entries carry ``before``/``after`` hashes
    for audit).
    """
    added = sorted(set(current) - set(baseline))
    removed = sorted(set(baseline) - set(current))
    modified = [
        {
            "path": path,
            "before_sha256": baseline[path]["sha256"],
            "after_sha256": current[path]["sha256"],
        }
        for path in sorted(set(baseline) & set(current))
        if baseline[path]["sha256"] != current[path]["sha256"]
    ]
    return {"added": added, "removed": removed, "modified": modified}


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="evolution_config_mutation_scan.py",
        description=(
            "Baseline + diff audit over the agent config/skill/hook trees "
            "(issue #89 slice A)."
        ),
    )
    parser.add_argument(
        "--init",
        action="store_true",
        help="write the baseline snapshot and exit (no diff)",
    )
    parser.add_argument(
        "--root",
        action="append",
        default=[],
        metavar="DIR",
        help=f"tree to snapshot (repeatable; default: {Path.home() / '.hermes'})",
    )
    parser.add_argument(
        "--project-root",
        default="",
        metavar="DIR",
        help="optional project tree to snapshot alongside HERMES_HOME",
    )
    parser.add_argument(
        "--baseline",
        default=str(DEFAULT_BASELINE),
        help=f"baseline JSON path (default: {DEFAULT_BASELINE})",
    )
    parser.add_argument(
        "--report",
        default=str(DEFAULT_REPORT),
        help=f"JSON report output path (default: {DEFAULT_REPORT})",
    )
    parser.add_argument(
        "--fail-on-changes",
        action="store_true",
        help="exit 2 when any mutation is detected (report-only by default)",
    )
    return parser.parse_args(argv[1:])


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(list(argv) if argv is not None else sys.argv)

    roots = [Path(r).expanduser() for r in args.root] or [
        Path.home() / ".hermes"
    ]
    if args.project_root:
        roots.append(Path(args.project_root).expanduser())

    baseline_path = Path(args.baseline).expanduser()
    report_path = Path(args.report).expanduser()

    current = snapshot_roots(roots)

    if args.init or not baseline_path.is_file():
        try:
            baseline_path.parent.mkdir(parents=True, exist_ok=True)
            baseline_path.write_text(
                json.dumps(
                    {
                        "scanned_at": datetime.now(UTC).isoformat(),
                        "roots": [str(r) for r in roots],
                        "files": current,
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            print(f"[config-mutation-scan] cannot write baseline: {exc}", file=sys.stderr)
            return 1
        print(
            f"[config-mutation-scan] baseline initialized: {len(current)} files "
            f"({', '.join(str(r) for r in roots)}) -> {baseline_path}"
        )
        return 0

    try:
        baseline_data = json.loads(baseline_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[config-mutation-scan] cannot read baseline: {exc}", file=sys.stderr)
        return 1
    baseline = baseline_data.get("files", {})

    mutations = detect_mutations(baseline, current)
    summary = {
        "scanned_at": datetime.now(UTC).isoformat(),
        "baseline": str(baseline_path),
        "roots": [str(r) for r in roots],
        "files_baselined": len(baseline),
        "files_current": len(current),
        **mutations,
        "changed_total": len(mutations["added"])
        + len(mutations["removed"])
        + len(mutations["modified"]),
    }

    try:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        print(f"[config-mutation-scan] cannot write report: {exc}", file=sys.stderr)
        return 1

    print(
        f"[config-mutation-scan] baselined={summary['files_baselined']} "
        f"current={summary['files_current']} "
        f"added={len(summary['added'])} removed={len(summary['removed'])} "
        f"modified={len(summary['modified'])} report={report_path}"
    )
    if args.fail_on_changes and summary["changed_total"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
