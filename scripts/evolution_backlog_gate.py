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

Cap resolution: --cap arg > env EVOLUTION_FEATURE_BACKLOG_CAP > dynamic cap
from metrics.jsonl > DEFAULT_CAP.  The dynamic cap shrinks when integration is
stuck so the backlog does not keep growing while the pipeline cannot land work.
Pure functions are import-safe for unit tests (the gh call is injected).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple

DEFAULT_CAP = 25

# Repo is resolved the same way the rest of the evolution tooling does.
_REPO = "Lexus2016/hermes-agent-evolution"


def _default_evolution_dir() -> Path:
    return Path(
        os.environ.get(
            "EVOLUTION_PROFILE_DIR",
            str(Path.home() / ".hermes" / "evolution"),
        )
    )


def _load_records(metrics_file: Path) -> list[Dict[str, Any]]:
    """Read metrics.jsonl-style records (one JSON object per line), skipping
    blank/malformed lines. Kept local so the gate has no import dependency on
    evolution_funnel at runtime."""
    out: list[Dict[str, Any]] = []
    if not metrics_file.exists():
        return out
    for ln in metrics_file.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            obj = json.loads(ln)
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _merged_zero_streak(records: list[Dict[str, Any]]) -> int:
    """Trailing run of cycles with merged == 0."""
    streak = 0
    for r in reversed(records):
        if int(r.get("merged", 0) or 0) == 0:
            streak += 1
        else:
            break
    return streak


def _cycle_success_rate(records: list[Dict[str, Any]]) -> float:
    """Fraction of active cycles (selected > 0 or issues_created > 0) that landed
    >=1 merge over the trailing window."""
    active = [
        r
        for r in records
        if int(r.get("selected", 0) or 0) > 0
        or int(r.get("issues_created", 0) or 0) > 0
    ]
    if not active:
        return 1.0
    succeeded = sum(1 for r in active if int(r.get("merged", 0) or 0) > 0)
    return succeeded / len(active)


def resolve_dynamic_cap(
    records: list[Dict[str, Any]],
    base_cap: int = DEFAULT_CAP,
) -> int:
    """Shrink the feature-backlog cap when integration is stuck.

    Logic mirrors the health sidecar's consolidation signal:
      - merged-zero streak >= 5                -> cap 5  (deep freeze)
      - merged-zero streak >= 3 or success rate < 1/3 -> cap 8 (consolidation)
      - otherwise                              -> base_cap
    Bugs are NEVER throttled by this adjustment.
    """
    streak = _merged_zero_streak(records)
    window = records[-14:] if len(records) >= 7 else records
    success_rate = _cycle_success_rate(window)

    if streak >= 5:
        return 5
    if streak >= 3 or success_rate < 1 / 3:
        return 8
    return base_cap


def resolve_cap(
    arg_cap: int | None = None,
    records: list[Dict[str, Any]] | None = None,
    evolution_dir: Path | None = None,
) -> int:
    """Cap resolution: --cap arg > env EVOLUTION_FEATURE_BACKLOG_CAP > dynamic
    cap from metrics.jsonl > DEFAULT_CAP.

    The dynamic path only fires when neither an explicit --cap nor the env
    override is set, keeping existing overrides intact.
    """
    if arg_cap is not None:
        return arg_cap
    env = os.environ.get("EVOLUTION_FEATURE_BACKLOG_CAP", "").strip()
    if env:
        try:
            return int(env)
        except ValueError:
            pass
    if records is not None:
        return resolve_dynamic_cap(records, DEFAULT_CAP)
    if evolution_dir is None:
        evolution_dir = _default_evolution_dir()
    records = _load_records(evolution_dir / "metrics.jsonl")
    return resolve_dynamic_cap(records, DEFAULT_CAP)


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
        "gh",
        "issue",
        "list",
        "--repo",
        _REPO,
        "--state",
        "open",
        "--limit",
        "300",
        "--json",
        "number,title,labels",
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
        return {
            "open_features": None,
            "cap": cap,
            "throttle": False,
            "note": "gh unavailable; defaulting to no throttle",
        }
    n = count_open_features(issues)
    return {"open_features": n, "cap": cap, "throttle": should_throttle(n, cap)}


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Throttle FEATURE proposals when the open backlog is full "
        "(bugs are never throttled)."
    )
    parser.add_argument("action", choices=["check"], help="check the gate")
    parser.add_argument(
        "--cap",
        type=int,
        default=None,
        help=f"feature-backlog cap (default {DEFAULT_CAP} / "
        f"env EVOLUTION_FEATURE_BACKLOG_CAP)",
    )
    parser.add_argument(
        "--evolution-dir",
        type=Path,
        default=None,
        help="directory holding metrics.jsonl for dynamic cap",
    )
    args = parser.parse_args(argv)

    evolution_dir = args.evolution_dir or _default_evolution_dir()
    result = evaluate(resolve_cap(args.cap, evolution_dir=evolution_dir))
    print(json.dumps(result))
    # exit 1 = THROTTLE (skip features), 0 = OK to create features.
    return 1 if result["throttle"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
