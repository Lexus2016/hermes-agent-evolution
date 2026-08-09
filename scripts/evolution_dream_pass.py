#!/usr/bin/env python3
"""Dream pass — grade-weighted memory retention for the evolution pipeline.

Phase 1 of issue #1870/#1875: a deterministic, no-LLM scheduled pass that
reads recent evolution cycle outcomes from ``metrics.jsonl`` and adjusts
tqmemory notes to reflect their quality. High-grade cycles get their notes
promoted to global scope; revision-needed cycles get their notes deprecated
with a failure-mode tag.

The pass reads the last N cycles from ``metrics.jsonl``, classifies each as
'high-grade' / 'revision-needed' / 'neutral', and applies adjustments via
direct file operations on the tqmemory notes directory (no MCP dependency).

Usage: python scripts/evolution_dream_pass.py [--max-cycles N]
"""

from __future__ import annotations
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List


def _evolution_dir() -> Path:
    """Resolve the evolution directory (profile-aware)."""
    env = os.environ.get("EVOLUTION_PROFILE_DIR", "").strip()
    if env:
        return Path(env)
    hermes_home = Path(
        os.environ.get("HERMES_HOME", "").strip() or (Path.home() / ".hermes")
    )
    return hermes_home / "evolution"


def _hermes_home() -> Path:
    return _evolution_dir().parent


def load_recent_metrics(max_cycles: int = 10) -> List[Dict[str, Any]]:
    """Load the last N entries from metrics.jsonl."""
    metrics_path = _evolution_dir() / "metrics.jsonl"
    if not metrics_path.exists():
        return []
    lines = (
        metrics_path.read_text(encoding="utf-8", errors="replace").strip().splitlines()
    )
    entries: List[Dict[str, Any]] = []
    for line in lines[-max_cycles:]:
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def classify_cycle(entry: Dict[str, Any]) -> str:
    """Classify a cycle: 'high-grade', 'revision-needed', or 'neutral'."""
    merged = int(entry.get("merged", 0) or 0)
    skipped = int(entry.get("skipped", 0) or 0)
    rejected = int(entry.get("rejected", 0) or 0)
    selected = int(entry.get("selected", 0) or 0)
    if merged > 0 and skipped == 0 and rejected <= 2:
        return "high-grade"
    if skipped > 0 or rejected > 5:
        return "revision-needed"
    if selected > 0 and merged == 0:
        return "revision-needed"
    return "neutral"


def find_cycle_notes(date: str) -> List[str]:
    """Find tqmemory note IDs associated with a given cycle date."""
    for subdir in ("tqmemory/notes", "turbo_quant_memory/notes"):
        notes_dir = _hermes_home() / subdir
        if notes_dir.exists():
            break
    else:
        return []
    note_ids: List[str] = []
    for note_file in notes_dir.glob("*.json"):
        try:
            data = json.loads(note_file.read_text(encoding="utf-8"))
            source_refs = data.get("source_refs", [])
            if any(f"evolution-cycle-{date}" in str(ref) for ref in source_refs):
                note_ids.append(data.get("id", note_file.stem))
        except (json.JSONDecodeError, OSError):
            continue
    return note_ids


def adjust_note_grade(note_id: str, grade: str, date: str) -> bool:
    """Adjust a tqmemory note based on its cycle grade. Returns True if adjusted."""
    if grade == "neutral":
        return False
    # Direct file-based adjustment (no MCP dependency).
    for subdir in ("tqmemory/notes", "turbo_quant_memory/notes"):
        notes_dir = _hermes_home() / subdir
        if notes_dir.exists():
            break
    else:
        return False
    note_file = notes_dir / f"{note_id}.json"
    if not note_file.exists():
        return False
    try:
        data = json.loads(note_file.read_text(encoding="utf-8"))
        if grade == "high-grade":
            data["scope"] = "global"
        elif grade == "revision-needed":
            data["deprecated"] = True
            data["deprecation_reason"] = f"grade-failure-mode:cycle-{date}"
        note_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return True
    except (json.JSONDecodeError, OSError):
        return False


def run_dream_pass(max_cycles: int = 10) -> Dict[str, Any]:
    """Run one dreaming pass: classify recent cycles and adjust notes."""
    metrics = load_recent_metrics(max_cycles)
    if not metrics:
        return {"status": "noop", "reason": "no metrics found", "adjusted": 0}
    summary: Dict[str, Any] = {
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
        grade = classify_cycle(entry)
        key = grade.replace("-", "_")
        summary[key] = summary.get(key, 0) + 1
        for nid in find_cycle_notes(date):
            try:
                adjusted = adjust_note_grade(nid, grade, date)
                if adjusted and grade == "high-grade":
                    summary["notes_promoted"] += 1
                elif adjusted and grade == "revision-needed":
                    summary["notes_deprecated"] += 1
            except Exception:
                summary["errors"] += 1
    result_path = _evolution_dir() / "dream-pass-result.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def main(argv: list[str] | None = None) -> int:
    max_cycles = 10
    args = (argv or sys.argv)[1:]
    if "--max-cycles" in args:
        i = args.index("--max-cycles")
        if i + 1 < len(args):
            try:
                max_cycles = int(args[i + 1])
            except ValueError:
                pass
    try:
        result = run_dream_pass(max_cycles)
        print(json.dumps(result, indent=2))
        return 0
    except Exception as exc:
        print(f"dream pass failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
