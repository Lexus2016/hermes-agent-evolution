#!/usr/bin/env python3
"""Action-conditioned memory verification, first slice (#3514, AMV-Guard).

Verification cost is priced by the action about to execute, not by the memory
entry. A consequence class decides whether the memory premises a call rests on
must be re-checked before it runs:

  * routine_read  — reads change nothing; premises are trusted as retrieved
  * write         — mutates state; every premise must be fresh
  * irreversible  — publish, send, delete, merge; every premise must be fresh

"Fresh" here means the premise carries an explicit confirmation marker. This
generalizes the remembered-proposal rule (a proposal stays unacknowledged until
the user confirms it) to the call boundary, without a premise-extraction
prompt — the caller supplies the one or two premises.

Usage:
  python scripts/evolution_action_memory_verify.py \
      --tool write_file --premise "proposal: deploy nightly (confirmed 2026-10-25)"
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Iterable, Optional

ROUTINE_READ = "routine_read"
WRITE = "write"
IRREVERSIBLE = "irreversible"

CONSEQUENCE_CLASSES = {
    # routine reads — no re-check
    "read_file": ROUTINE_READ,
    "search_files": ROUTINE_READ,
    "web_search": ROUTINE_READ,
    "web_extract": ROUTINE_READ,
    "repo_map": ROUTINE_READ,
    # writes — re-check premises
    "write_file": WRITE,
    "patch": WRITE,
    "terminal": WRITE,
    # irreversible — re-check premises
    "merge": IRREVERSIBLE,
    "git_merge": IRREVERSIBLE,
    "send_message": IRREVERSIBLE,
    "delete": IRREVERSIBLE,
}

RECHECK_REQUIRED = {WRITE, IRREVERSIBLE}

# A premise is fresh only when it says so. Mirrors the remembered-proposal
# rule: unconfirmed memory is not grounds for a consequential call.
_FRESH_MARKERS = ("confirmed", "verified", "user-acknowledged")


def consequence_class(tool_name: str) -> str:
    """Class of a tool call. Unknown tools are writes: re-check by default."""
    return CONSEQUENCE_CLASSES.get(tool_name.strip().lower(), WRITE)


def premise_is_fresh(premise: str) -> bool:
    """True when the premise text carries an explicit confirmation marker."""
    text = premise.lower()
    return any(marker in text for marker in _FRESH_MARKERS)


def verify_action(tool_name: str, premises: Iterable[str]) -> dict:
    """Decide whether a call may proceed given the premises it rests on.

    Routine reads proceed unconditionally. Writes and irreversible calls
    proceed only when every supplied premise is fresh; no premises at all is
    a hold, because there is nothing to re-check.
    """
    klass = consequence_class(tool_name)
    premise_list = [p.strip() for p in premises if p and p.strip()]
    result = {
        "tool": tool_name,
        "consequence_class": klass,
        "premises_checked": len(premise_list),
    }
    if klass not in RECHECK_REQUIRED:
        result.update(proceed=True, reason="routine_read_no_recheck")
        return result
    if not premise_list:
        result.update(proceed=False, reason="no_premises_to_recheck")
        return result
    stale = [p for p in premise_list if not premise_is_fresh(p)]
    if stale:
        result.update(proceed=False, reason="stale_premises", stale_premises=stale)
        return result
    result.update(proceed=True, reason="premises_fresh")
    return result


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Action-conditioned memory verification (#3514)"
    )
    parser.add_argument("--tool", required=True, help="tool about to be called")
    parser.add_argument(
        "--premise",
        action="append",
        default=[],
        help="memory premise (repeatable, max 2)",
    )
    args = parser.parse_args(argv)

    result = verify_action(args.tool, args.premise[:2])
    print(json.dumps(result))
    return 0 if result["proceed"] else 1


if __name__ == "__main__":
    sys.exit(main())
