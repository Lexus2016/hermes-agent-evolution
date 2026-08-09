#!/usr/bin/env python3
"""COVE volatility-tagged memory gate (#1938). Pure deterministic heuristics."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

VOLATILE, STABLE, STRATEGIC = "volatile", "stable", "strategic"
_URL = re.compile(r"https?://\S+|www\.\S+\.\S+")
_VERSION = re.compile(r"\bv?\d+\.\d+(?:\.\d+){0,2}\b")
_ALGO = re.compile(
    r"\b(binary search|dfs|bfs|dynamic programming|merge sort|quicksort|recursion|invariant|complexity|o\(?n\b)",
    re.I,
)
_PLAN = re.compile(
    r"\b(plan|next step|debug|reproduce|hypothesis|try then|todo|investigate|fallback)\b",
    re.I,
)


def classify(content: str) -> str:
    """urls/versions→volatile, algo→stable, plan→strategic."""
    if not content:
        return STABLE
    if _PLAN.search(content) and _ALGO.search(content):
        return STRATEGIC
    if _URL.search(content) or _VERSION.search(content):
        return VOLATILE
    if _ALGO.search(content):
        return STABLE
    if _PLAN.search(content):
        return STRATEGIC
    return STABLE


def _ip(index_path: str | None = None) -> Path:
    if index_path:
        return Path(index_path)
    return (
        Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
        / "evolution"
        / "volatility-index.json"
    )


def _load(index_path: str | None = None) -> dict:
    p = _ip(index_path)
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save(idx: dict, index_path: str | None = None) -> None:
    p = _ip(index_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(idx, indent=2, sort_keys=True), encoding="utf-8")


def tag(note_id: str, content: str, index_path: str | None = None) -> dict:
    """Classify a note and record its volatility level in the index."""
    level = classify(content)
    idx = _load(index_path)
    idx[note_id] = level
    _save(idx, index_path)
    return {"id": note_id, "level": level}


def list_notes(index_path: str | None = None) -> dict:
    return _load(index_path)


def check(durable_path: str, index_path: str | None = None) -> dict:
    """Anti-recitation: detect volatile-tagged values hardcoded into durable files."""
    idx = _load(index_path)
    try:
        text = Path(durable_path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {"path": durable_path, "violations": [], "ok": True}
    hits = []
    for nid, lvl in idx.items():
        if lvl != VOLATILE:
            continue
        hits += [
            {"note_id": nid, "kind": "url", "value": m.group(0)[:80]}
            for m in _URL.finditer(text)
        ]
        hits += [
            {"note_id": nid, "kind": "version", "value": m.group(0)}
            for m in _VERSION.finditer(text)
        ]
    return {"path": durable_path, "violations": hits, "ok": not hits}


def _cli(argv: list[str]) -> int:
    if not argv or argv[0] not in ("tag", "check", "list"):
        print(
            "usage: evolution_volatility_gate.py {tag|check|list} ...", file=sys.stderr
        )
        return 2
    ip = os.environ.get("VOLATILITY_INDEX")
    if argv[0] == "tag":
        if len(argv) < 3:
            print("usage: tag <note-id> <content>", file=sys.stderr)
            return 2
        print(json.dumps(tag(argv[1], argv[2], ip)))
    elif argv[0] == "list":
        print(json.dumps(list_notes(ip), indent=2))
    elif argv[0] == "check":
        if len(argv) < 2:
            print("usage: check <durable-path>", file=sys.stderr)
            return 2
        r = check(argv[1], ip)
        print(json.dumps(r, indent=2))
        return 0 if r["ok"] else 1
    return 0
