#!/usr/bin/env python3
"""Evolution idea dedup cache — O(new) instead of re-comparing all history.

evolution-issues / evolution-introspection must not re-propose an idea this
install has already filed or had rejected. The naive check pulls up to 300
issues every run and compares each proposal by meaning in-context — a cost that
grows forever with repo age (#91).

This maintains a tiny local cache keyed on the NORMALIZED proposal title:

    ~/.hermes/evolution/dedup-cache.json
    { "<key>": {"title": "...", "status": "filed|rejected|considered",
                "issue": 123, "date": "YYYY-MM-DD"} }

A proposal whose key is already in the cache is skipped in O(1) — no gh query,
no in-context comparison. Only cache-MISSES fall back to the (smaller) `gh issue
list` query to catch ideas filed by OTHER installs; their outcome is then
recorded so the next run short-circuits. The cache is purely a NEGATIVE
fast-path: it never causes a false "new" (a miss just means "do the gh check"),
so it can never make us file a dup the gh fallback would have caught.

CLI (so the skill can call it from the terminal tool):
    evolution_dedup.py check  "<title>"            # exit 0 = NEW, 1 = already seen
    evolution_dedup.py record "<title>" <status> [issue] [date]
Pure functions are import-safe for unit tests.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict

# Tag prefixes the pipeline puts on titles, e.g. "[FIX] ", "[IMPROVEMENT] ".
_TAG_RE = re.compile(r"^\s*\[[^\]]+\]\s*")
_NONWORD_RE = re.compile(r"[^a-z0-9]+")
_MAX_ENTRIES = 5000  # cap so the cache file can't grow unbounded


def normalize_title(title: str) -> str:
    """Canonicalize a proposal title for meaning-stable matching.

    Strips a leading ``[TAG]``, lowercases, drops punctuation, and collapses
    whitespace so cosmetic edits ("Fix the X" vs "[FIX] fix  the X.") map to the
    same key. Deliberately simple + deterministic (no stemming) — false
    NEGATIVES (two phrasings -> different keys) are safe: the gh fallback still
    runs for a miss. False positives (different ideas -> same key) would be bad,
    so we keep the normalized form information-rich.
    """
    s = _TAG_RE.sub("", title or "")
    s = s.lower().strip()
    s = _NONWORD_RE.sub(" ", s).strip()
    return s


def idea_key(title: str) -> str:
    norm = normalize_title(title)
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16]


def load_cache(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_cache(path: Path, cache: Dict[str, Any]) -> None:
    # Cap: drop oldest entries (by date) if over the limit, so the file stays small.
    if len(cache) > _MAX_ENTRIES:
        items = sorted(cache.items(), key=lambda kv: str(kv[1].get("date", "")))
        cache = dict(items[-_MAX_ENTRIES:])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def is_seen(cache: Dict[str, Any], title: str) -> bool:
    return idea_key(title) in cache


def record(cache: Dict[str, Any], title: str, status: str, issue: Any = None, date: str = "") -> Dict[str, Any]:
    """Add/update an entry. Returns the cache (mutated in place)."""
    key = idea_key(title)
    entry: Dict[str, Any] = {"title": title, "status": status}
    if issue:
        entry["issue"] = issue
    if date:
        entry["date"] = date
    cache[key] = entry
    return cache


def _cache_path() -> Path:
    return Path(
        os.environ.get(
            "EVOLUTION_PROFILE_DIR",
            str(Path.home() / ".hermes" / "evolution"),
        )
    ) / "dedup-cache.json"


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print("usage: evolution_dedup.py check|record \"<title>\" [status] [issue] [date]", file=sys.stderr)
        return 2
    action, title = argv[1], argv[2]
    path = _cache_path()
    cache = load_cache(path)

    if action == "check":
        seen = is_seen(cache, title)
        print("seen" if seen else "new")
        return 1 if seen else 0
    if action == "record":
        status = argv[3] if len(argv) > 3 else "considered"
        issue = argv[4] if len(argv) > 4 else None
        date = argv[5] if len(argv) > 5 else ""
        record(cache, title, status, issue=issue, date=date)
        save_cache(path, cache)
        print(f"recorded {idea_key(title)} status={status}")
        return 0
    print(f"unknown action: {action}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
