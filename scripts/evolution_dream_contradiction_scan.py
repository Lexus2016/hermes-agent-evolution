#!/usr/bin/env python3
"""Cron entry point for the dream contradiction scanner (issue #48, slice 1).

Scheduled as the ``no_agent`` job ``evolution-dream-contradiction-scan``
(``cron/evolution/dream-contradiction-scan.yaml``, 02:45 daily — 45 minutes
after the 02:00 dreaming-pipeline / dream-consolidation jobs finish writing
tqmemory notes, so contradictions are flagged within 24h of the conflicting
entries appearing).

What it does (all deterministic, no LLM, no MCP):

1. reads every note in the live tqmemory filesystem store
   (``~/.turbo-quant-memory/projects/*/notes/*.json``),
2. groups ACTIVE notes by normalized title and flags same-title groups with
   more than one active note — the newest (by ``created_at``) is authoritative,
   the older ones are deprecation-linked,
3. writes the deprecation record exactly as tqmemory's ``deprecate_note``
   would (``note_status=superseded``, ``deprecated_at``, ``deprecation_reason``,
   ``superseded_by``) so the MCP server and the filesystem stay consistent,
4. writes a JSON report to ``~/.hermes/evolution/dream_contradiction_scan.json``
   and prints a one-line summary (deliver: local — captured by the cron
   scheduler, never spammed to a channel).

Pass ``--dry-run`` to scan and report without writing any deprecations
(used by tests and by any manual inspection).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# The scheduler executes scripts from HERMES_HOME/scripts; the helper family
# is installed there as a unit (register_evolution_cron.py), so the import
# below resolves at runtime exactly as it does in the repo checkout.
from evolution_dream_pass import TQMEMORY_DEFAULT_ROOT, dream_contradiction_scan

DEFAULT_REPORT = Path.home() / ".hermes" / "evolution" / "dream_contradiction_scan.json"


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="evolution_dream_contradiction_scan.py",
        description=(
            "Deterministic contradiction scan over the live tqmemory note "
            "store (Oracular Dream, issue #48 slice 1)."
        ),
    )
    parser.add_argument(
        "--root",
        default=str(TQMEMORY_DEFAULT_ROOT),
        help="tqmemory root (default: ~/.turbo-quant-memory)",
    )
    parser.add_argument(
        "--report",
        default=str(DEFAULT_REPORT),
        help="JSON report output path (default: ~/.hermes/evolution/dream_contradiction_scan.json)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="scan and report only; do NOT write deprecations",
    )
    return parser.parse_args(argv[1:])


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(list(argv) if argv is not None else sys.argv)
    try:
        summary = dream_contradiction_scan(Path(args.root), dry_run=args.dry_run)
    except Exception as exc:
        print(f"[dream-contradiction-scan] FAILED: {exc}", file=sys.stderr)
        return 1

    report = Path(args.report)
    try:
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        print(f"[dream-contradiction-scan] cannot write report: {exc}", file=sys.stderr)
        return 1

    mode = "DRY-RUN" if args.dry_run else "applied"
    print(
        f"[dream-contradiction-scan] notes={summary['notes_scanned']} "
        f"active={summary['active_notes']} contradictions="
        f"{summary['contradictions_found']} deprecations_{mode}="
        f"{summary['deprecations_applied']} report={report}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
