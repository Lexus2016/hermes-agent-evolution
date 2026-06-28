#!/usr/bin/env python3
"""Generation backlog gate — throttle FEATURE proposals when the board is full.

The evolution pipeline generates ~25 issues/day (research + issues +
introspection) but the processing chain lands only a few/day, so without a cap
the open backlog grows unbounded ("again many unprocessed issues").

This gate lets the generation stages decide whether to SKIP creating new
FEATURE / IMPROVEMENT proposals when the open *feature* backlog is already at or
above a cap. BUGS are NEVER throttled — a real defect ([FIX] / `bug`) must
always be filed regardless of backlog, since unfiled bugs block work and are
cheap to keep.

A "feature" open issue = open AND not a bug:
  * title does NOT start with ``[FIX]`` (case-insensitive), AND
  * labels do NOT include ``bug``.

CLI (so a skill can call it from the terminal tool):
    evolution_backlog_gate.py check            # exit 0 = OK to create features,
                                               # exit 1 = THROTTLE (skip features)
    evolution_backlog_gate.py check --cap 30   # override the cap

Prints a one-line JSON summary on stdout either way:
    {"open_features": 42, "cap": 25, "throttle": true}

Cap resolution: --cap arg > env EVOLUTION_FEATURE_BACKLOG_CAP > DEFAULT_CAP.
Pure functions are import-safe for unit tests (the gh call is injected).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from typing import Any, Callable, Dict, List, Tuple

DEFAULT_CAP = 25

# Repo is resolved the same way the rest of the evolution tooling does.
_REPO = "Lexus2016/hermes-agent-evolution"


def resolve_cap(arg_cap: int | None = None) -> int:
    if arg_cap is not None:
        return arg_cap
    env = os.environ.get("EVOLUTION_FEATURE_BACKLOG_CAP", "").strip()
    if env:
        try:
            return int(env)
        except ValueError:
            pass
    return DEFAULT_CAP


def is_bug(issue: Dict[str, Any]) -> bool:
    """True when an issue is a bug/[FIX] (never throttled)."""
    title = (issue.get("title") or "").lstrip()
    if title.upper().startswith("[FIX]"):
        return True
    labels = issue.get("labels") or []
    names = {
        (lbl.get("name") if isinstance(lbl, dict) else str(lbl)).lower()
        for lbl in labels
    }
    return "bug" in names


def count_open_features(issues: List[Dict[str, Any]]) -> int:
    """Count open issues that are FEATURE-like (i.e. not bugs)."""
    return sum(1 for it in issues if not is_bug(it))


def should_throttle(open_features: int, cap: int) -> bool:
    """Throttle once the feature backlog reaches the cap."""
    return open_features >= cap


def _default_runner(cmd: List[str]) -> Tuple[int, str]:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    return proc.returncode, (proc.stdout or "")


def fetch_open_issues(
    runner: Callable[[List[str]], Tuple[int, str]] | None = None,
) -> List[Dict[str, Any]] | None:
    """Return the list of open issues, or None if gh failed (fail-open)."""
    runner = runner or _default_runner
    rc, out = runner([
        "gh", "issue", "list", "--repo", _REPO,
        "--state", "open", "--limit", "300",
        "--json", "number,title,labels",
    ])
    if rc != 0:
        return None
    try:
        data = json.loads(out)
        return data if isinstance(data, list) else None
    except (ValueError, TypeError):
        return None


def evaluate(
    cap: int,
    runner: Callable[[List[str]], Tuple[int, str]] | None = None,
) -> Dict[str, Any]:
    """Compute the gate decision. Fail-OPEN (throttle=False) if gh is unavailable
    — never block bug/feature generation just because the count couldn't be read."""
    issues = fetch_open_issues(runner)
    if issues is None:
        return {"open_features": None, "cap": cap, "throttle": False,
                "note": "gh unavailable; defaulting to no throttle"}
    n = count_open_features(issues)
    return {"open_features": n, "cap": cap, "throttle": should_throttle(n, cap)}


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Throttle FEATURE proposals when the open backlog is full "
                    "(bugs are never throttled)."
    )
    parser.add_argument("action", choices=["check"], help="check the gate")
    parser.add_argument("--cap", type=int, default=None,
                        help=f"feature-backlog cap (default {DEFAULT_CAP} / "
                             f"env EVOLUTION_FEATURE_BACKLOG_CAP)")
    args = parser.parse_args(argv)

    result = evaluate(resolve_cap(args.cap))
    print(json.dumps(result))
    # exit 1 = THROTTLE (skip features), 0 = OK to create features.
    return 1 if result["throttle"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
