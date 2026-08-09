#!/usr/bin/env python3
"""Grade-weighted dream pass for the evolution pipeline (#1875, child of #1870).

Phase 1 of grade-weighted memory retention ("dreaming"): reads recent cycle
outcomes from ``metrics.jsonl`` and adjusts a file-backed note store so
high-grade runs get promoted (weight raised) and revision-needed runs get a
failure-mode tag. Pure file-based — no LLM, no MCP dependency.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

PROMOTE_BUMP = 0.5  # weight increment for high-grade cycles
WEIGHT_CAP = 2.0


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read a JSONL file (one JSON object per line), skipping blank/malformed."""
    out: list[dict[str, Any]] = []
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        s = raw.strip()
        try:
            obj = json.loads(s)
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def load_records(metrics_file: Path) -> list[dict[str, Any]]:
    """Read metrics.jsonl records (one JSON object per line)."""
    return _load_jsonl(metrics_file)


def load_notes(notes_file: Path) -> list[dict[str, Any]]:
    """Read the file-backed note store (one JSON object per line)."""
    return _load_jsonl(notes_file)


def classify_cycle(rec: dict[str, Any]) -> str:
    """high-grade: >=1 merged and ratio >=0.5; revision-needed: 0 merged & >=1
    rejected; else neutral."""
    merged = int(rec.get("merged", 0) or 0)
    selected = int(rec.get("selected", 0) or 0)
    rejected = int(rec.get("rejected", 0) or 0)
    ratio = merged / selected if selected else 0.0
    if merged >= 1 and ratio >= 0.5:
        return "high-grade"
    if merged == 0 and rejected >= 1:
        return "revision-needed"
    return "neutral"


def dream_pass(
    metrics_file: Path, notes_file: Path, *, recent: int = 7
) -> dict[str, Any]:
    """Run one grade-weighted dream pass; write ``dream_pass.json`` summary.

    Promotes notes whose ``cycle`` matches a high-grade record (raise weight);
    tags notes for revision-needed cycles with ``failure:unmerged``.
    """
    records = load_records(metrics_file)[-recent:]
    grades = {r.get("date", "?"): classify_cycle(r) for r in records}
    by_grade = {
        g: [d for d, k in grades.items() if k == g]
        for g in ("high-grade", "revision-needed", "neutral")
    }
    notes = load_notes(notes_file)
    promoted = tagged = 0
    g = by_grade
    for n in notes:
        tags = n.setdefault("tags", [])
        if n.get("cycle") in g["high-grade"]:
            w = min(WEIGHT_CAP, float(n.get("weight", 1.0)) + PROMOTE_BUMP)
            n["weight"] = round(w, 2)
            if "promoted" not in tags:
                tags.append("promoted")
            promoted += 1
        elif n.get("cycle") in g["revision-needed"]:
            if "failure:unmerged" not in tags:
                tags.append("failure:unmerged")
            tagged += 1
    if notes:
        txt = "".join(json.dumps(n) + "\n" for n in notes)
        notes_file.write_text(txt, encoding="utf-8")
    summary = {
        "cycles_reviewed": len(records),
        "high_grade": by_grade["high-grade"],
        "revision_needed": by_grade["revision-needed"],
        "neutral": by_grade["neutral"],
        "notes_promoted": promoted,
        "notes_tagged": tagged,
    }
    (metrics_file.parent / "dream_pass.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return summary


if __name__ == "__main__":  # pragma: no cover
    import sys

    evo = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("evolution")
    dream_pass(evo / "metrics.jsonl", evo / "notes.jsonl")
