#!/usr/bin/env python3
"""Nightly sleeper-poisoning audit over persistent memory stores (issue #101, slice A).

Scheduled as the ``no_agent`` job ``evolution-memory-poison-scan``
(``cron/evolution/memory-poison-scan.yaml``, 03:30 daily — 90 minutes after the
02:00 dreaming pipeline / dream-consolidation jobs finish writing tqmemory
notes, so freshly written notes are covered within 24h).

Background: "Hidden in Memory: Sleeper Memory Poisoning in LLM Agents"
(Pulipaka et al., arXiv:2605.15338) demonstrates a write-path attack where an
adversary manipulates external context so the agent stores a fabricated memory;
when retrieved days later it drives attacker-intended agentic actions. The
read-time untrusted-content boundary does not cover the write path. This is
slice A of the defense: a deterministic, read-only audit that flags the
injection markers an adversary would need to smuggle into a memory store —
zero-width/bidi control characters (hidden text), "ignore previous
instructions" phrasing, and fake ``system:``/``assistant:`` roleplay lines —
so a poisoned write is surfaced within 24h.

What it does (all deterministic, no LLM, no MCP, no writes to the stores):

1. scans the file-based persistent memory stores: ``~/.hermes/memories/*.md``
   (MEMORY.md / USER.md), ``~/.turbo-quant-memory/projects/*/notes/*.json``
   (tqmemory notes, the Oracular Dream store), and ``~/.hermes/mem0.json``
   (mem0 store),
2. checks every line against the SAME marker table the evolution extract chain
   already uses (``evolution_extract.looks_like_injection`` /
   ``has_hidden_chars``) so the two gates agree on what an injection looks
   like,
3. writes a JSON report to ``~/.hermes/evolution/memory_poison_scan.json`` and
   prints a one-line summary (deliver: local — a scan run is data, not an
   alert).

Read-only by design: findings are reported, never auto-deleted (auto-remediation
is a later slice and a product decision). ``--fail-on-findings`` makes the exit
code actionable for a future alerting wrapper.

Slice A deliberately does NOT touch the write path (provenance tagging /
quarantine is slice B) — it turns the poisoning threat into a detectable,
regression-testable signal first.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# The scheduler executes scripts from HERMES_HOME/scripts; the helper family
# is installed there as a unit (register_evolution_cron.py), so the import
# below resolves at runtime exactly as it does in the repo checkout.
from evolution_extract import has_hidden_chars, looks_like_injection

# Default targets: the file-based persistent memory stores on a standard
# HERMES install. A directory target is walked recursively for *.md / *.json,
# skipping dot-directories (.snapshots, .git, __pycache__) and the tqmemory
# vault. The tqmemory target is the per-project ``notes/`` subdirectory — NOT
# the whole projects tree, which also holds the markdown-index and retrieval
# (LanceDB) machinery and the encrypted secrets vault (1G+ of churn and
# material that is not memory content).

_SKIP_DIRS = {".git", ".snapshots", "__pycache__", ".venv", "node_modules", "secrets"}
_TEXT_EXTS = {".md", ".json", ".txt", ".yaml", ".yml"}

_SNIPPET_MAX = 140  # keep report snippets short enough to be human-scannable

DEFAULT_REPORT = Path.home() / ".hermes" / "evolution" / "memory_poison_scan.json"


def default_targets() -> list[Path]:
    """The default store set: HERMES memories, mem0, and every tqmemory project's
    ``notes/`` subdirectory (the Oracular Dream note store)."""
    home = Path.home()
    targets: list[Path] = [
        home / ".hermes" / "memories",
        home / ".hermes" / "mem0.json",
    ]
    projects = home / ".turbo-quant-memory" / "projects"
    if projects.is_dir():
        targets.extend(sorted(projects.glob("*/notes")))
    return targets


def _iter_target_files(targets: list[Path]) -> list[Path]:
    """Expand targets to a sorted, deduplicated list of files to scan."""
    seen: set[Path] = set()
    files: list[Path] = []
    for target in targets:
        if target.is_file():
            if target not in seen:
                seen.add(target)
                files.append(target)
        elif target.is_dir():
            for ext in _TEXT_EXTS:
                for path in sorted(target.rglob(f"*{ext}")):
                    if any(part in _SKIP_DIRS for part in path.parts):
                        continue
                    if not path.is_file():
                        continue
                    if path not in seen:
                        seen.add(path)
                        files.append(path)
    return sorted(files, key=lambda p: str(p))


def scan_file(path: Path) -> list[dict[str, Any]]:
    """Scan ONE file line-by-line for injection markers.

    Returns a list of findings::

        {"path": str, "line": int, "marker": str, "snippet": str}

    ``marker`` is ``hidden_chars`` (zero-width / bidi control characters) or
    ``instruction_shape`` (an ignore/disregard-previous-instructions phrase or
    a fake ``system:``/``assistant:``/``developer:`` roleplay line). Empty for
    clean files.
    """
    findings: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return findings  # unreadable store file is not an injection finding
    for idx, line in enumerate(text.splitlines(), start=1):
        if has_hidden_chars(line):
            findings.append(
                {
                    "path": str(path),
                    "line": idx,
                    "marker": "hidden_chars",
                    "snippet": line.strip()[:_SNIPPET_MAX],
                }
            )
        elif looks_like_injection(line):
            findings.append(
                {
                    "path": str(path),
                    "line": idx,
                    "marker": "instruction_shape",
                    "snippet": line.strip()[:_SNIPPET_MAX],
                }
            )
    return findings


def scan_memory_stores(targets: list[Path]) -> dict[str, Any]:
    """Run the audit over the expanded target set.

    Returns a summary dict (the report payload)::

        {"scanned_at", "files_scanned", "files_with_findings", "findings": [...]}
    """
    findings: list[dict[str, Any]] = []
    files_with_findings = 0
    files_scanned = 0
    for path in _iter_target_files(targets):
        files_scanned += 1
        file_findings = scan_file(path)
        if file_findings:
            files_with_findings += 1
            findings.extend(file_findings)
    return {
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "files_scanned": files_scanned,
        "files_with_findings": files_with_findings,
        "findings": findings,
    }


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="evolution_memory_poison_scan.py",
        description=(
            "Deterministic nightly audit for sleeper-poisoning injection "
            "markers over the persistent memory stores (issue #101 slice A)."
        ),
    )
    parser.add_argument(
        "--target",
        action="append",
        default=[],
        metavar="PATH",
        help=(
            "file or directory to scan (repeatable; a directory is walked "
            "recursively for *.md/*.json). Default: the HERMES memories dir, "
            "the tqmemory projects dir and mem0.json."
        ),
    )
    parser.add_argument(
        "--report",
        default=str(DEFAULT_REPORT),
        help=f"JSON report output path (default: {DEFAULT_REPORT})",
    )
    parser.add_argument(
        "--fail-on-findings",
        action="store_true",
        help="exit 2 when findings exist (report-only by default)",
    )
    return parser.parse_args(argv[1:])


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(list(argv) if argv is not None else sys.argv)
    targets = [Path(t).expanduser() for t in args.target] or default_targets()
    try:
        summary = scan_memory_stores(targets)
    except Exception as exc:  # noqa: BLE001 - a scheduled job must fail loudly
        print(f"[memory-poison-scan] FAILED: {exc}", file=sys.stderr)
        return 1

    report = Path(args.report)
    try:
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        print(f"[memory-poison-scan] cannot write report: {exc}", file=sys.stderr)
        return 1

    print(
        f"[memory-poison-scan] files={summary['files_scanned']} "
        f"files_with_findings={summary['files_with_findings']} "
        f"findings={len(summary['findings'])} report={report}"
    )
    if args.fail_on_findings and summary["findings"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
