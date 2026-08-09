#!/usr/bin/env python3
"""Append-only evolution event log — Evolver genomic protocol (issue #1940).

Records every evolution event (proposal, decision, implementation, revert,
merge, reject) in an append-only JSONL log for auditability and rollback
safety. Each event carries a type classification (innovation / optimization /
repair) enabling strategy-aware analytics.

Also provides strategy preset selection: when the backlog is full (>= 25 open
issues) bias toward ``repair-only`` (0/20/80); when clear, use ``balanced``
(50/30/20). The backlog gate already exists — this wires it to a strategy
selector that the analysis stage reads.

Usage:
    python scripts/evolution_event_log.py [--evolution-dir DIR] [log <event-json>]
    python scripts/evolution_event_log.py [--evolution-dir DIR] strategy [--open N]

Output:
    Appends to ``evolution_events.jsonl`` in the evolution directory.
"""

from __future__ import annotations
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# Strategy presets: innovation / optimization / repair ratio.
STRATEGY_PRESETS: Dict[str, Dict[str, int]] = {
    "balanced": {"innovation": 50, "optimization": 30, "repair": 20},
    "harden": {"innovation": 20, "optimization": 40, "repair": 40},
    "repair-only": {"innovation": 0, "optimization": 20, "repair": 80},
}

# Backlog threshold above which we switch to repair-only.
_BACKLOG_FULL_THRESHOLD = 25

_VALID_TYPES = {"innovation", "optimization", "repair"}
_VALID_ACTIONS = {"proposal", "decision", "implementation", "revert", "merge", "reject"}


def _default_evolution_dir() -> Path:
    env = os.environ.get("EVOLUTION_PROFILE_DIR", "").strip()
    if env:
        return Path(env)
    hermes_home = Path(
        os.environ.get("HERMES_HOME", "").strip() or (Path.home() / ".hermes")
    )
    return hermes_home / "evolution"


def select_strategy(open_issue_count: int) -> str:
    """Pick a strategy preset based on backlog size.

    Full backlog -> repair-only (fix what's broken first).
    Clear backlog -> balanced (explore new features).
    """
    if open_issue_count >= _BACKLOG_FULL_THRESHOLD:
        return "repair-only"
    return "balanced"


def classify_evolution_type(labels: List[str]) -> str:
    """Classify an issue by its evolution type from labels.

    bug/research-generated bugs -> repair; enhancement -> innovation or
    optimization (heuristic: 'improvement' label -> optimization, else
    innovation).
    """
    label_set = {l.lower() for l in (labels or [])}
    if "bug" in label_set:
        return "repair"
    if "improvement" in label_set:
        return "optimization"
    return "innovation"


def log_event(
    event: Dict[str, Any],
    evolution_dir: Path | None = None,
) -> Dict[str, Any]:
    """Append a typed evolution event to the append-only JSONL log.

    Required fields are filled with defaults if missing. Returns the
    finalized event dict that was written.
    """
    if evolution_dir is None:
        evolution_dir = _default_evolution_dir()
    action = event.get("action", "proposal")
    if action not in _VALID_ACTIONS:
        raise ValueError(f"invalid action '{action}'; must be one of {_VALID_ACTIONS}")
    ev_type = event.get("evolution_type", "innovation")
    if ev_type not in _VALID_TYPES:
        raise ValueError(
            f"invalid evolution_type '{ev_type}'; must be one of {_VALID_TYPES}"
        )
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
        evolution_dir = _default_evolution_dir()
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
    """Summarize events by action and evolution_type for analytics."""
    by_action: Dict[str, int] = {}
    by_type: Dict[str, int] = {}
    for e in events:
        a = e.get("action", "unknown")
        t = e.get("evolution_type", "unknown")
        by_action[a] = by_action.get(a, 0) + 1
        by_type[t] = by_type.get(t, 0) + 1
    return {"total": len(events), "by_action": by_action, "by_type": by_type}


def main(argv: List[str]) -> int:
    evolution_dir: Path | None = None
    args = argv[1:]
    # Parse --evolution-dir
    if "--evolution-dir" in args:
        i = args.index("--evolution-dir")
        if i + 1 < len(args):
            evolution_dir = Path(args[i + 1])
            args = args[:i] + args[i + 2 :]
    if not args:
        # Default: print summary
        events = load_events(evolution_dir)
        s = event_summary(events)
        print(
            f"[event-log] {s['total']} events: by_action={s['by_action']} by_type={s['by_type']}"
        )
        return 0
    sub = args[0]
    if sub == "strategy":
        open_n = int(args[1]) if len(args) > 1 else 0
        preset = select_strategy(open_n)
        print(f"[event-log] strategy={preset} ratios={STRATEGY_PRESETS[preset]}")
        return 0
    if sub == "log":
        if len(args) < 2:
            print(
                "error: log subcommand requires a JSON event argument", file=sys.stderr
            )
            return 1
        event = json.loads(args[1])
        result = log_event(event, evolution_dir)
        print(f"[event-log] logged: {result['action']}/{result['evolution_type']}")
        return 0
    print(f"unknown subcommand: {sub}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
