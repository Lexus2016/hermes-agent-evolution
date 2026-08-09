#!/usr/bin/env python3
"""Genomic evolution protocol — strategy presets + typed event log.

Adds structured vocabulary to the evolution pipeline:
- Strategy presets (innovation/optimization/repair ratio) selected
  by backlog fullness via the existing backlog gate.
- Append-only JSONL event log (evolution_events.jsonl) with typed
  entries (proposal, decision, implementation, revert) and evolution
  type tags (innovation, optimization, repair).

Pure functions, no LLM, no network. Import-safe.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

HERMES_HOME = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.home() / ".hermes"
EVOLUTION_DIR = HERMES_HOME / "evolution"
EVENT_LOG = EVOLUTION_DIR / "evolution_events.jsonl"

STRATEGY_PRESETS: dict[str, dict[str, int]] = {
    "balanced": {"innovation": 50, "optimization": 30, "repair": 20},
    "harden": {"innovation": 20, "optimization": 40, "repair": 40},
    "repair-only": {"innovation": 0, "optimization": 20, "repair": 80},
}

VALID_EVENT_TYPES = {"proposal", "decision", "implementation", "revert"}
VALID_EVOLUTION_TYPES = {"innovation", "optimization", "repair"}


def select_strategy(open_issues: int, cap: int = 100) -> str:
    """Select strategy preset based on backlog fullness.
    Full backlog → repair-only; clear → balanced."""
    if open_issues >= cap * 0.8:
        return "repair-only"
    if open_issues >= cap * 0.5:
        return "harden"
    return "balanced"


def get_preset(name: str) -> dict[str, int]:
    """Return the strategy preset ratios. Raises KeyError if unknown."""
    if name not in STRATEGY_PRESETS:
        raise KeyError(f"Unknown strategy: {name}. Valid: {list(STRATEGY_PRESETS)}")
    return STRATEGY_PRESETS[name]


def classify_evolution_type(issue_title: str) -> str:
    """Classify an issue/proposal by evolution type from its title."""
    t = issue_title.lower()
    if "[fix]" in t or "[bug]" in t or "fix" in t.split()[:2]:
        return "repair"
    if "[improvement]" in t or "optimize" in t or "refactor" in t:
        return "optimization"
    return "innovation"


def log_event(
    event_type: str,
    evolution_type: str,
    description: str,
    issue_number: int | None = None,
    event_log: Path | None = None,
) -> dict:
    """Append a typed event to the evolution event log (JSONL).
    Returns the event dict that was logged."""
    if event_type not in VALID_EVENT_TYPES:
        raise ValueError(f"Invalid event_type: {event_type}")
    if evolution_type not in VALID_EVOLUTION_TYPES:
        raise ValueError(f"Invalid evolution_type: {evolution_type}")
    event = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event_type": event_type,
        "evolution_type": evolution_type,
        "description": description,
        "issue_number": issue_number,
    }
    log = event_log or EVENT_LOG
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")
    return event


def load_events(event_log: Path | None = None) -> list[dict]:
    """Read all events from the JSONL log."""
    log = event_log or EVENT_LOG
    if not log.exists():
        return []
    events = []
    for line in log.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                events.append(json.loads(line))
            except ValueError:
                pass
    return events


def summary(events: list[dict]) -> dict:
    """Return analytics summary of events by type."""
    by_event: dict[str, int] = {}
    by_evo: dict[str, int] = {}
    for e in events:
        by_event[e["event_type"]] = by_event.get(e["event_type"], 0) + 1
        by_evo[e["evolution_type"]] = by_evo.get(e["evolution_type"], 0) + 1
    return {
        "total": len(events),
        "by_event_type": by_event,
        "by_evolution_type": by_evo,
    }
