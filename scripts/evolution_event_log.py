#!/usr/bin/env python3
"""Append-only evolution event log — Evolver genomic protocol (issue #1940).

Records evolution events in an append-only JSONL log for auditability.
Provides strategy preset selection: >=25 open issues → repair-only,
else balanced.

Usage:
    python scripts/evolution_event_log.py [--evolution-dir DIR] [log <json>]
    python scripts/evolution_event_log.py [--evolution-dir DIR] strategy [--open N]
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

STRATEGY_PRESETS: Dict[str, Dict[str, int]] = {
    "balanced": {"innovation": 50, "optimization": 30, "repair": 20},
    "harden": {"innovation": 20, "optimization": 40, "repair": 40},
    "repair-only": {"innovation": 0, "optimization": 20, "repair": 80},
}
_THRESHOLD = 25
_TYPES = {"innovation", "optimization", "repair"}
_ACTIONS = {"proposal", "decision", "implementation", "revert", "merge", "reject"}


def _evolution_dir() -> Path:
    env = os.environ.get("EVOLUTION_PROFILE_DIR", "").strip()
    if env:
        return Path(env)
    home = os.environ.get("HERMES_HOME", "").strip() or (Path.home() / ".hermes")
    return Path(home) / "evolution"


def select_strategy(open_issue_count: int) -> str:
    """Pick strategy preset: full backlog → repair-only, clear → balanced."""
    return "repair-only" if open_issue_count >= _THRESHOLD else "balanced"


def classify_evolution_type(labels: List[str]) -> str:
    """Classify an issue: bug → repair, improvement → optimization, else innovation."""
    ls = {l.lower() for l in (labels or [])}
    if "bug" in ls:
        return "repair"
    if "improvement" in ls:
        return "optimization"
    return "innovation"


def log_event(
    event: Dict[str, Any], evolution_dir: Path | None = None
) -> Dict[str, Any]:
    """Append a typed evolution event to the append-only JSONL log."""
    if evolution_dir is None:
        evolution_dir = _evolution_dir()
    action = event.get("action", "proposal")
    if action not in _ACTIONS:
        raise ValueError(f"invalid action '{action}'")
    ev_type = event.get("evolution_type", "innovation")
    if ev_type not in _TYPES:
        raise ValueError(f"invalid evolution_type '{ev_type}'")
    finalized = {
        "timestamp": event.get("timestamp") or datetime.now(timezone.utc).isoformat(),
        "action": action,
        "evolution_type": ev_type,
        "issue_number": event.get("issue_number"),
        "pr_number": event.get("pr_number"),
        "description": event.get("description", ""),
        "metadata": event.get("metadata", {}),
    }
    log_path = evolution_dir / "evolution_events.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(finalized, sort_keys=True) + "\n")
    return finalized


def load_events(evolution_dir: Path | None = None) -> List[Dict[str, Any]]:
    """Read all events from the append-only log, oldest-first."""
    if evolution_dir is None:
        evolution_dir = _evolution_dir()
    log_path = evolution_dir / "evolution_events.jsonl"
    if not log_path.exists():
        return []
    events: List[Dict[str, Any]] = []
    for ln in log_path.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            obj = json.loads(ln)
        except ValueError:
            continue
        if isinstance(obj, dict):
            events.append(obj)
    return events


def event_summary(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Summarize events by action and evolution_type."""
    ba: Dict[str, int] = {}
    bt: Dict[str, int] = {}
    for e in events:
        for key, target in (("action", ba), ("evolution_type", bt)):
            k = e.get(key, "?")
            target[k] = target.get(k, 0) + 1
    return {"total": len(events), "by_action": ba, "by_type": bt}


def main(argv: List[str]) -> int:
    evolution_dir: Path | None = None
    args = argv[1:]
    if "--evolution-dir" in args:
        i = args.index("--evolution-dir")
        if i + 1 < len(args):
            evolution_dir = Path(args[i + 1])
            args = args[:i] + args[i + 2 :]
    if not args:
        s = event_summary(load_events(evolution_dir))
        print(
            f"[event-log] {s['total']} events: by_action={s['by_action']} by_type={s['by_type']}"
        )
        return 0
    if args[0] == "strategy":
        open_n = int(args[1]) if len(args) > 1 else 0
        p = select_strategy(open_n)
        print(f"[event-log] strategy={p} ratios={STRATEGY_PRESETS[p]}")
        return 0
    if args[0] == "log":
        if len(args) < 2:
            print("error: log requires a JSON event argument", file=sys.stderr)
            return 1
        result = log_event(json.loads(args[1]), evolution_dir)
        print(f"[event-log] logged: {result['action']}/{result['evolution_type']}")
        return 0
    print(f"unknown subcommand: {args[0]}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
