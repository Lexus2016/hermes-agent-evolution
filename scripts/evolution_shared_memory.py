#!/usr/bin/env python3
"""Shared read-only memory tier for multi-instance experience sharing.

Phase 1 of issue #1873/#1877: a deterministic pass that retrieves
cross-instance learnings from the tqmemory ``global`` scope and caches
them locally so evolution cycles can benefit from other instances'
experiences (e.g. "web-extract fails on JS-heavy pages") even when the
tqmemory MCP server is temporarily unavailable.

The tqmemory server already supports ``scope="global"`` as a broader
visibility tier — ``promote_note`` moves a note from ``project`` to
``global``. This script makes the global tier useful for cross-instance
sharing by:

1. Querying ``semantic_search(scope="global", source_filter="notes")``
   for evolution-tagged notes from any instance.
2. Caching the results to ``~/.hermes/evolution/shared-memory-cache.json``
   so cycles can consult them offline.
3. Providing a ``retrieve_shared_experiences()`` function that the
   evolution pipeline calls to enrich its context with cross-instance
   lessons.

Read-only by design — this instance never writes to the global scope
directly (promotion is handled by the dream pass, issue #1875). Multiple
instances reading the same global scope never conflict because reads are
non-destructive.

Exit codes: 0 on success (including no-op when tqmemory is offline), 1 on
unexpected failure.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))


def _evolution_dir() -> Path:
    return _hermes_home() / "evolution"


def _cache_path() -> Path:
    return _evolution_dir() / "shared-memory-cache.json"


def _query_global_scope(
    query: str = "evolution lesson pattern", limit: int = 20
) -> List[Dict[str, Any]]:
    """Query the tqmemory global scope for cross-instance notes.

    Tries the tqmemory MCP server's Python API first, then falls back to
    reading the global notes directory directly. Returns a list of note
    dicts with at minimum {id, title, content, tags, source_refs}.
    """
    # Path 1: direct Python import of tqmemory server
    try:
        sys.path.insert(0, os.path.expanduser("~/.hermes/tqmemory/src"))
        from turbo_memory_mcp.server import semantic_search_impl  # type: ignore

        results = semantic_search_impl(
            query=query,
            scope="global",
            limit=limit,
            source_filter="notes",
            cwd=str(_hermes_home()),
        )
        if isinstance(results, str):
            results = json.loads(results)
        if isinstance(results, list):
            return results
        if isinstance(results, dict) and "results" in results:
            return results["results"]
        return []
    except Exception:
        pass  # MCP server not available — fall through to filesystem

    # Path 2: filesystem fallback — read global notes directly
    global_notes_dir = _hermes_home() / "tqmemory" / "global" / "notes"
    if not global_notes_dir.exists():
        global_notes_dir = _hermes_home() / "turbo_quant_memory" / "global" / "notes"
    if not global_notes_dir.exists():
        return []

    notes = []
    for note_file in global_notes_dir.glob("*.json"):
        try:
            data = json.loads(note_file.read_text(encoding="utf-8"))
            tags = data.get("tags", [])
            # Only include evolution-tagged notes for cross-instance sharing
            if any("evolution" in str(t).lower() for t in tags):
                notes.append({
                    "id": data.get("id", note_file.stem),
                    "title": data.get("title", ""),
                    "content": data.get("content", ""),
                    "tags": tags,
                    "source_refs": data.get("source_refs", []),
                    "kind": data.get("note_kind", data.get("kind", "")),
                })
        except (json.JSONDecodeError, OSError):
            continue
    return notes[:limit]


def retrieve_shared_experiences(query: str = "evolution") -> List[Dict[str, Any]]:
    """Retrieve cross-instance experiences from the shared memory tier.

    Called by evolution cycles to enrich context with lessons other
    instances have learned. Returns cached results if tqmemory is offline.
    """
    # Try live query first
    results = _query_global_scope(query=query)
    if results:
        # Update cache
        _write_cache(results)
        return results

    # Fall back to cache
    return _read_cache()


def _read_cache() -> List[Dict[str, Any]]:
    """Read the shared memory cache file."""
    cache = _cache_path()
    if not cache.exists():
        return []
    try:
        data = json.loads(cache.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data.get("notes", [])
        if isinstance(data, list):
            return data
    except json.JSONDecodeError:
        pass
    return []


def _write_cache(notes: List[Dict[str, Any]]) -> None:
    """Write notes to the shared memory cache file."""
    cache = _cache_path()
    cache.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "note_count": len(notes),
        "notes": notes,
    }
    cache.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def run_shared_memory_sync() -> Dict[str, Any]:
    """Run one shared-memory sync pass: query global scope, update cache.

    Returns a summary dict. This is the entry point for the cron job.
    """
    notes = _query_global_scope()
    _write_cache(notes)
    return {
        "status": "ok",
        "notes_synced": len(notes),
        "cached": len(notes) > 0,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def main() -> int:
    try:
        result = run_shared_memory_sync()
        print(json.dumps(result, indent=2))
        return 0
    except Exception as exc:
        print(f"shared memory sync failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
