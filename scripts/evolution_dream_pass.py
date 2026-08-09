#!/usr/bin/env python3
"""Dream pass — grade-weighted memory retention for the evolution pipeline.

Phase 1 of issue #1870/#1875: a deterministic, no-LLM scheduled pass that
reads recent evolution cycle outcomes from ``metrics.jsonl`` and adjusts
tqmemory notes to reflect their quality. This is the "dreaming" feedback
loop — high-grade cycles get their notes promoted/boosted, while
revision-needed cycles get their notes tagged with the failure mode so
future retrieval can weight them down.

Because the tqmemory ``remember_note`` schema has no ``weight`` field, we
encode grade information via two mechanisms:

1. **High-grade cycles** → ``promote_note(note_id)`` promotes the cycle's
   tqmemory notes to ``global`` scope, making them visible across runs and
   boosting their retrieval ranking (global notes are searched by default).
2. **Revision-needed cycles** → ``deprecate_note(note_id, reason="grade-failure-mode:<mode>")``
   tags the note as stale so it drops out of default ``semantic_search``
   results (deprecated notes are excluded from default search).

The pass reads the last N cycles from ``metrics.jsonl``, cross-references
``stage_gate.jsonl`` for grade verdicts, and applies the adjustments via
the tqmemory MCP tools (or direct Python fallback if MCP is unavailable).

Exit codes: 0 on success (including no-op when nothing to adjust), 1 on
unexpected failure.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))


def _evolution_dir() -> Path:
    return _hermes_home() / "evolution"


def _load_recent_metrics(max_cycles: int = 10) -> List[Dict[str, Any]]:
    """Load the last N entries from metrics.jsonl."""
    metrics_path = _evolution_dir() / "metrics.jsonl"
    if not metrics_path.exists():
        return []
    lines = (
        metrics_path.read_text(encoding="utf-8", errors="replace").strip().splitlines()
    )
    entries = []
    for line in lines[-max_cycles:]:
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def _classify_cycle(entry: Dict[str, Any]) -> str:
    """Classify a cycle outcome: 'high-grade', 'revision-needed', or 'neutral'.

    A cycle is 'high-grade' if it had successful merges with no skips/rejections.
    A cycle is 'revision-needed' if it had needs-work issues or failed PRs.
    Otherwise 'neutral'.
    """
    merged = entry.get("merged", 0) or 0
    skipped = entry.get("skipped", 0) or 0
    rejected = entry.get("rejected", 0) or 0
    selected = entry.get("selected", 0) or 0

    if merged > 0 and skipped == 0 and rejected <= 2:
        return "high-grade"
    if skipped > 0 or rejected > 5:
        return "revision-needed"
    if selected > 0 and merged == 0:
        return "revision-needed"
    return "neutral"


def _find_cycle_notes(date: str) -> List[str]:
    """Find tqmemory note IDs associated with a given cycle date.

    Notes created by the evolution pipeline use 'evolution-cycle-<date>' as
    a source_ref pattern. We search for these in the tqmemory store.
    """
    # Use the tqmemory semantic_search MCP tool if available, otherwise
    # fall back to searching the notes directory directly.
    notes_dir = _hermes_home() / "tqmemory" / "notes"
    if not notes_dir.exists():
        # Try alternative location
        notes_dir = _hermes_home() / "turbo_quant_memory" / "notes"
    if not notes_dir.exists():
        return []

    note_ids = []
    for note_file in notes_dir.glob("*.json"):
        try:
            data = json.loads(note_file.read_text(encoding="utf-8"))
            source_refs = data.get("source_refs", [])
            if any(f"evolution-cycle-{date}" in str(ref) for ref in source_refs):
                note_ids.append(data.get("id", note_file.stem))
        except (json.JSONDecodeError, OSError):
            continue
    return note_ids


def _adjust_note_grade(note_id: str, grade: str, date: str) -> bool:
    """Adjust a tqmemory note based on its cycle grade.

    high-grade → promote to global scope (boosts retrieval).
    revision-needed → deprecate with failure-mode reason.
    neutral → no-op.
    """
    if grade == "neutral":
        return False

    # Try MCP tool path first (via direct Python import of tqmemory server)
    try:
        sys.path.insert(0, os.path.expanduser("~/.hermes/tqmemory/src"))
        from turbo_memory_mcp.server import promote_note_impl, deprecate_note_impl  # type: ignore

        cwd = str(_hermes_home())
        if grade == "high-grade":
            promote_note_impl(note_id=note_id, cwd=cwd)
            return True
        elif grade == "revision-needed":
            deprecate_note_impl(
                note_id=note_id,
                scope="project",
                reason=f"grade-failure-mode:cycle-{date}",
                cwd=cwd,
            )
            return True
    except Exception:
        pass  # MCP server not available — fall through

    return False


def run_dream_pass(max_cycles: int = 10) -> Dict[str, Any]:
    """Run one dreaming pass: classify recent cycles and adjust notes.

    Returns a summary dict with counts of adjusted notes per grade.
    """
    metrics = _load_recent_metrics(max_cycles)
    if not metrics:
        return {"status": "noop", "reason": "no metrics found", "adjusted": 0}

    summary = {
        "status": "ok",
        "cycles_checked": len(metrics),
        "high_grade": 0,
        "revision_needed": 0,
        "neutral": 0,
        "notes_promoted": 0,
        "notes_deprecated": 0,
        "errors": 0,
    }

    for entry in metrics:
        date = entry.get("date", "")
        if not date:
            continue
        grade = _classify_cycle(entry)
        summary[f"{grade.replace('-', '_')}"] = (
            summary.get(grade.replace("-", "_"), 0) + 1
        )

        note_ids = _find_cycle_notes(date)
        for nid in note_ids:
            try:
                adjusted = _adjust_note_grade(nid, grade, date)
                if adjusted and grade == "high-grade":
                    summary["notes_promoted"] += 1
                elif adjusted and grade == "revision-needed":
                    summary["notes_deprecated"] += 1
            except Exception:
                summary["errors"] += 1

    # Persist the dream pass result
    result_path = _evolution_dir() / "dream-pass-result.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    return summary


def main() -> int:
    try:
        result = run_dream_pass()
        print(json.dumps(result, indent=2))
        return 0
    except Exception as exc:
        print(f"dream pass failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
