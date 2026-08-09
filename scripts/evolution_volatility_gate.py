#!/usr/bin/env python3
"""Volatility-tagged memory with anti-recitation (issue #1938, COVE).

Tags notes with {volatile, stable, strategic}. Volatile values (API URLs,
version strings) are never baked into durable skills. Provides anti-recitation
guard and A/B release test for safe note internalization.

Usage: evolution_volatility_gate.py [--evolution-dir DIR] tag <note-id> <vol>
       evolution_volatility_gate.py [--evolution-dir DIR] check <content>
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List

_LEVELS = {"volatile", "stable", "strategic"}
_PATTERNS = [
    re.compile(r"https?://[^\s\"')]+", re.I),
    re.compile(r"\b(v\d+\.\d+(?:\.\d+)?)\b"),
    re.compile(r"\b(api|endpoint|schema)[-_]?(name|key|version)\b", re.I),
]


def _path(d: Path | None = None) -> Path:
    if d is not None:
        return d / "volatility_index.json"
    env = os.environ.get("EVOLUTION_PROFILE_DIR", "").strip()
    home = env or os.environ.get("HERMES_HOME", "").strip() or (Path.home() / ".hermes")
    return Path(home) / "evolution" / "volatility_index.json"


def load_index(d: Path | None = None) -> Dict[str, str]:
    """Load note-id → volatility-level mapping."""
    p = _path(d)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return (
            {k: v for k, v in data.items() if v in _LEVELS}
            if isinstance(data, dict)
            else {}
        )
    except (json.JSONDecodeError, OSError):
        return {}


def tag_note(note_id: str, vol: str, d: Path | None = None) -> Dict[str, str]:
    """Tag a note with a volatility level. Returns the updated index."""
    if vol not in _LEVELS:
        raise ValueError(f"invalid volatility '{vol}'; must be one of {_LEVELS}")
    idx = load_index(d)
    idx[note_id] = vol
    p = _path(d)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(idx, indent=2, sort_keys=True), encoding="utf-8")
    return idx


def classify_volatility(content: str) -> str:
    """Classify content: volatile if patterns detected, else stable."""
    return "volatile" if any(p.search(content) for p in _PATTERNS) else "stable"


def anti_recitation_check(
    content: str, idx: Dict[str, str] | None = None
) -> List[Dict[str, Any]]:
    """Check if volatile values are being hardcoded into a durable file."""
    warnings: List[Dict[str, Any]] = []
    vi = idx or {}
    for nid in [n for n, v in vi.items() if v == "volatile"]:
        if nid in content:
            warnings.append({
                "type": "volatile_recitation",
                "note_id": nid,
                "message": f"volatile note '{nid}' appears hardcoded",
            })
    for p in _PATTERNS:
        for m in p.finditer(content):
            warnings.append({
                "type": "volatile_pattern",
                "match": m.group(),
                "message": f"volatile pattern '{m.group()}' in durable file",
            })
    return warnings


def ab_release_test(
    note_id: str, q_before: float, q_after: float, threshold: float = 0.05
) -> Dict[str, Any]:
    """A/B release test: after internalizing a stable note, check if quality dropped."""
    delta = q_before - q_after
    can = delta < threshold
    return {
        "note_id": note_id,
        "delta": round(delta, 4),
        "can_archive": can,
        "decision": "archive" if can else "restore",
    }


def list_notes(vol: str | None = None, d: Path | None = None) -> Dict[str, str]:
    """List notes optionally filtered by volatility level."""
    idx = load_index(d)
    if vol:
        if vol not in _LEVELS:
            raise ValueError(f"invalid volatility '{vol}'")
        return {k: v for k, v in idx.items() if v == vol}
    return idx


def volatility_summary(idx: Dict[str, str]) -> Dict[str, int]:
    counts = {"volatile": 0, "stable": 0, "strategic": 0}
    for v in idx.values():
        if v in counts:
            counts[v] += 1
    counts["total"] = len(idx)
    return counts


def main(argv: List[str]) -> int:
    d: Path | None = None
    args = argv[1:]
    if "--evolution-dir" in args:
        i = args.index("--evolution-dir")
        if i + 1 < len(args):
            d = Path(args[i + 1])
            args = args[:i] + args[i + 2 :]
    if not args:
        s = volatility_summary(load_index(d))
        print(
            f"[volatility] {s['total']}: vol={s['volatile']} stable={s['stable']} strat={s['strategic']}"
        )
        return 0
    if args[0] == "tag" and len(args) >= 3:
        tag_note(args[1], args[2], d)
        print(f"[volatility] tagged {args[1]} as {args[2]}")
        return 0
    if args[0] == "check" and len(args) >= 2:
        print(json.dumps(anti_recitation_check(args[1], load_index(d)), indent=2))
        return 0
    if args[0] == "list":
        print(json.dumps(list_notes(args[1] if len(args) > 1 else None, d), indent=2))
        return 0
    if args[0] == "ab-release" and len(args) >= 4:
        print(
            json.dumps(
                ab_release_test(args[1], float(args[2]), float(args[3])), indent=2
            )
        )
        return 0
    print(f"unknown: {args[0]}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
