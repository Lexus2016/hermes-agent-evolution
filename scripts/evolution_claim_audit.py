#!/usr/bin/env python3
"""Post-hoc claim audit for a research draft (issue #3521).

Re-checks every checkable figure in the finished research report against the
evidence the report itself cites, and tags each one ``verified``,
``unverified`` or ``unattributed``. ``unverified`` means the number or date the
draft reports does not occur in the excerpt it cites for it — a fabricated or
drifted figure — and makes the command exit 1 so the stage cannot treat that
sentence as an established fact.

The ``evolution-issues`` stage runs this on the report it is about to turn into
issues: the research stage has no terminal, so this is the first point in the
pipeline where a deterministic check can run before the numbers are consumed.

Evidence sidecar (written by the research stage): a JSON object mapping each
cited URL to the verbatim excerpt the figures came from::

    {"https://arxiv.org/abs/2610.31372": "reports 85.2% F1 ... submitted 2026-10-22"}

Usage::

    evolution_claim_audit.py research/2026-10-26.md
    evolution_claim_audit.py research/2026-10-26.md --sources ev.json --json

Exit: 0 audit passed (or skipped: no sidecar), 1 unverified figures, 2 bad input.
No network, no LLM, no dependency.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evolution.lib.source_claim_audit import (
    audit_report,
    load_sources,
    render_summary,
)

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_BAD_INPUT = 2


def _default_sources_path(report: Path) -> Path:
    """``2026-10-26.md`` -> ``2026-10-26.sources.json`` beside it."""
    return report.with_name(report.stem + ".sources.json")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("report", help="research report markdown to audit")
    parser.add_argument(
        "--sources", help="evidence sidecar (default: <report>.sources.json)"
    )
    parser.add_argument(
        "--json", action="store_true", help="print the full result as JSON"
    )
    parser.add_argument(
        "--out", help="write the result JSON here (default: <report>.claim_audit.json)"
    )
    args = parser.parse_args(argv[1:])

    report = Path(args.report).expanduser()
    try:
        text = report.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"error: cannot read report {report}: {exc}", file=sys.stderr)
        return EXIT_BAD_INPUT

    sources_path = (
        Path(args.sources).expanduser()
        if args.sources
        else _default_sources_path(report)
    )
    result = audit_report(text, load_sources(sources_path))

    payload = result.as_dict()
    payload["report_file"] = report.name
    payload["sources_file"] = sources_path.name if sources_path.exists() else None
    out_path = (
        Path(args.out).expanduser()
        if args.out
        else report.with_name(report.stem + ".claim_audit.json")
    )
    try:
        out_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
        )
    except OSError as exc:
        print(f"warning: cannot write {out_path}: {exc}", file=sys.stderr)

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(render_summary(result))
        for claim in result.unverified:
            print(
                f"  UNVERIFIED {claim.figure} ({claim.kind}) cited {claim.url}: {claim.line[:160]}"
            )

    if result.skipped:
        return EXIT_OK
    return EXIT_OK if result.passed else EXIT_FAILED


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
