#!/usr/bin/env python3
"""Prompt/state-file integrity check (#98) — drift alert for long-lived files.

Hashes the long-lived prompt/state files (SOUL.md, MEMORY.md, USER.md) under
HERMES_HOME and compares them against the stored SHA-256 baseline.  Exits
non-zero when any file has drifted, so a cron job can alert on tampering.

Usage:
    prompt_integrity_check.py                 # check + alert (exit 1 on drift)
    prompt_integrity_check.py --json          # machine-readable report
    prompt_integrity_check.py --establish     # (re)baseline current contents
    prompt_integrity_check.py --home <dir>    # non-default HERMES_HOME

Deterministic and offline — no LLM calls, no network.  Content is hashed as
DATA and never parsed, so an embedded payload cannot influence this script.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import List, Optional

from agent.prompt_integrity import (
    PROTECTED_FILES,
    IntegrityReport,
    load_registry,
    verify_integrity,
)


def _resolve_home(home_arg: Optional[str]) -> Path:
    if home_arg:
        return Path(home_arg).expanduser()
    try:
        from hermes_constants import get_hermes_home

        return get_hermes_home()
    except Exception:
        env = __import__("os").environ.get("HERMES_HOME")
        if env:
            return Path(env).expanduser()
        return Path.home() / ".hermes"


def _report_dict(home: Path, report: IntegrityReport) -> dict:
    return {
        "home": str(home),
        "protected_files": list(PROTECTED_FILES),
        "established": report.established,
        "drifts": report.drifts,
        "ok": report.ok,
        "current": report.current,
        "baseline": load_registry(home),
    }


def _format_report(home: Path, report: IntegrityReport) -> str:
    lines: list[str] = [
        "=" * 60,
        f"Prompt-file integrity (#98): {home}",
        f"Protected files: {', '.join(PROTECTED_FILES)}",
    ]
    if report.established:
        lines.append("No baseline found — established a fresh one. No drift to report.")
    elif report.ok:
        lines.append("RESULT: OK — no drift from baseline.")
    else:
        for rel in report.drifts:
            lines.append(f"[DRIFT] {rel} — changed/added/removed since baseline.")
        lines.append("RESULT: DRIFT DETECTED")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument(
        "--establish",
        action="store_true",
        help="force a fresh baseline from current contents",
    )
    parser.add_argument("--home", default=None, help="HERMES_HOME to check")
    args = parser.parse_args(argv)

    home = _resolve_home(args.home)
    if args.establish:
        # Overwrite the stored registry with current hashes, then re-verify.
        from agent.prompt_integrity import store_registry, compute_hashes

        store_registry(home, compute_hashes(home))
    report = verify_integrity(home)

    if args.json:
        print(json.dumps(_report_dict(home, report), indent=2, ensure_ascii=False))
    else:
        print(_format_report(home, report))
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
