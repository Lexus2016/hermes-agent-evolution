#!/usr/bin/env python3
"""Volatility-tagged memory with anti-recitation (issue #1938, COVE).

Tags tqmemory notes with volatility labels {volatile, stable, strategic}:
- volatile: API endpoints, schema names, version-specific syntax — never
  baked into durable skills, always re-read at runtime.
- stable: reasoning patterns, algorithmic idioms — candidates to fold into
  skill files after repeated evidence.
- strategic: debugging plans — keep in memory, internalize after stability.

Provides an anti-recitation guard that warns when a volatile-tagged value
is about to be hardcoded into a durable artifact (skill file). Also provides
an A/B release test: after a skill-file update that internalizes a stable
note, remove the original note from retrieval and measure whether quality
drops.

Usage:
    python scripts/evolution_volatility_gate.py [--evolution-dir DIR] tag <note-id> <volatility>
    python scripts/evolution_volatility_gate.py [--evolution-dir DIR] check <file-content>
    python scripts/evolution_volatility_gate.py [--evolution-dir DIR] ab-release <note-id>
    python scripts/evolution_volatility_gate.py [--evolution-dir DIR] list [--volatility V]
"""

from __future__ import annotations
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

_VOLATILITY_LEVELS = {"volatile", "stable", "strategic"}
# Patterns that indicate volatile content (API endpoints, version strings, etc.).
_VOLATILE_PATTERNS = [
    re.compile(r"https?://[^\s\"')]+", re.I),  # URLs / API endpoints
    re.compile(r"\b(v\d+\.\d+(?:\.\d+)?)\b"),  # version strings like v2.1.0
    re.compile(r"\b(api|endpoint|schema)[-_]?(name|key|version)\b", re.I),
]


def _default_evolution_dir() -> Path:
    env = os.environ.get("EVOLUTION_PROFILE_DIR", "").strip()
    if env:
        return Path(env)
    hermes_home = Path(
        os.environ.get("HERMES_HOME", "").strip() or (Path.home() / ".hermes")
    )
    return hermes_home / "evolution"


def _volatility_index_path(evolution_dir: Path | None = None) -> Path:
    return (evolution_dir or _default_evolution_dir()) / "volatility_index.json"


def load_volatility_index(evolution_dir: Path | None = None) -> Dict[str, str]:
    """Load the note-id → volatility-level mapping."""
    path = _volatility_index_path(evolution_dir)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if v in _VOLATILITY_LEVELS}
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def save_volatility_index(
    index: Dict[str, str], evolution_dir: Path | None = None
) -> None:
    """Save the note-id → volatility-level mapping."""
    path = _volatility_index_path(evolution_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(index, indent=2, sort_keys=True), encoding="utf-8")


def tag_note(
    note_id: str, volatility: str, evolution_dir: Path | None = None
) -> Dict[str, str]:
    """Tag a note with a volatility level. Returns the updated index."""
    if volatility not in _VOLATILITY_LEVELS:
        raise ValueError(
            f"invalid volatility '{volatility}'; must be one of {_VOLATILITY_LEVELS}"
        )
    index = load_volatility_index(evolution_dir)
    index[note_id] = volatility
    save_volatility_index(index, evolution_dir)
    return index


def classify_content_volatility(content: str) -> str:
    """Heuristically classify content volatility by pattern matching.

    Returns 'volatile' if volatile patterns are detected, 'stable' otherwise.
    """
    for pattern in _VOLATILE_PATTERNS:
        if pattern.search(content):
            return "volatile"
    return "stable"


def anti_recitation_check(
    file_content: str,
    volatility_index: Dict[str, str] | None = None,
) -> List[Dict[str, Any]]:
    """Check if volatile-tagged values are being hardcoded into a durable file.

    Returns a list of warnings for each volatile value found.
    """
    warnings: List[Dict[str, Any]] = []
    vi = volatility_index or {}
    # Check for volatile note references being inlined.
    volatile_note_ids = [nid for nid, v in vi.items() if v == "volatile"]
    for nid in volatile_note_ids:
        if nid in file_content:
            warnings.append({
                "type": "volatile_recitation",
                "note_id": nid,
                "message": f"volatile note '{nid}' content appears to be hardcoded",
            })
    # Check for raw volatile patterns (URLs, versions) without re-read guards.
    for pattern in _VOLATILE_PATTERNS:
        for match in pattern.finditer(file_content):
            warnings.append({
                "type": "volatile_pattern",
                "match": match.group(),
                "message": f"volatile pattern '{match.group()}' found in durable file",
            })
    return warnings


def ab_release_test(
    note_id: str,
    quality_before: float,
    quality_after: float,
    threshold: float = 0.05,
) -> Dict[str, Any]:
    """A/B release test: after internalizing a stable note, check if quality
    dropped. If the delta is below threshold, the note can be archived.

    Returns a result dict with the decision.
    """
    delta = quality_before - quality_after
    can_archive = delta < threshold
    return {
        "note_id": note_id,
        "quality_before": quality_before,
        "quality_after": quality_after,
        "delta": round(delta, 4),
        "threshold": threshold,
        "can_archive": can_archive,
        "decision": "archive" if can_archive else "restore",
    }


def list_notes(
    volatility: str | None = None,
    evolution_dir: Path | None = None,
) -> Dict[str, str]:
    """List notes optionally filtered by volatility level."""
    index = load_volatility_index(evolution_dir)
    if volatility:
        if volatility not in _VOLATILITY_LEVELS:
            raise ValueError(f"invalid volatility '{volatility}'")
        return {k: v for k, v in index.items() if v == volatility}
    return index


def volatility_summary(index: Dict[str, str]) -> Dict[str, int]:
    """Summarize volatility index by level."""
    counts: Dict[str, int] = {"volatile": 0, "stable": 0, "strategic": 0}
    for v in index.values():
        if v in counts:
            counts[v] += 1
    counts["total"] = len(index)
    return counts


def main(argv: List[str]) -> int:
    evolution_dir: Path | None = None
    args = argv[1:]
    if "--evolution-dir" in args:
        i = args.index("--evolution-dir")
        if i + 1 < len(args):
            evolution_dir = Path(args[i + 1])
            args = args[:i] + args[i + 2 :]
    if not args:
        s = volatility_summary(load_volatility_index(evolution_dir))
        print(
            f"[volatility-gate] {s['total']} notes: volatile={s['volatile']} stable={s['stable']} strategic={s['strategic']}"
        )
        return 0
    sub = args[0]
    if sub == "tag":
        if len(args) < 3:
            print("error: tag requires <note-id> <volatility>", file=sys.stderr)
            return 1
        tag_note(args[1], args[2], evolution_dir)
        print(f"[volatility-gate] tagged {args[1]} as {args[2]}")
        return 0
    if sub == "check":
        if len(args) < 2:
            print("error: check requires <file-content>", file=sys.stderr)
            return 1
        warnings = anti_recitation_check(args[1], load_volatility_index(evolution_dir))
        print(json.dumps(warnings, indent=2))
        return 0
    if sub == "list":
        v = args[1] if len(args) > 1 else None
        print(json.dumps(list_notes(v, evolution_dir), indent=2))
        return 0
    if sub == "ab-release":
        if len(args) < 4:
            print(
                "error: ab-release requires <note-id> <quality-before> <quality-after>",
                file=sys.stderr,
            )
            return 1
        result = ab_release_test(args[1], float(args[2]), float(args[3]))
        print(json.dumps(result, indent=2))
        return 0
    print(f"unknown subcommand: {sub}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
