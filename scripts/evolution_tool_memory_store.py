#!/usr/bin/env python3
"""Tool-memory store — persistent capability/failure records (#1218).

Provides a JSON-backed store for tool-memory records that survive across
sessions.  Each record captures a tool's capability description, failure
boundaries, composition partners, and a last-verified timestamp.

WHY THIS EXISTS — ToolAtlas (parent #1178).
When the agent encounters a new MCP tool or skill, it currently has no
memory of what that tool can do, where it fails, or what it composes well
with.  This store provides the persistent substrate; later increments
(probing, wiring into tool_describe) populate and consume it.

DESIGN — minimal surface, zero core coupling.
``ToolMemoryStore`` is a standalone class backed by a JSON file under
``~/.hermes/evolution/tool-memory/``.  It provides read/write/query
functions with no dependency on the core agent loop.  The schema is
extensible — callers add records and the store validates the required
fields.

Record schema::

    {
        "tool_name": "terminal",
        "capability": "Execute shell commands in a sandboxed environment",
        "failure_boundaries": ["cannot interact with GUI apps", "no sudo"],
        "composition_partners": ["read_file", "write_file", "patch"],
        "last_verified": "2026-07-23T12:00:00Z",
        "metadata": {}
    }

Usage::

    store = ToolMemoryStore()
    store.put({"tool_name": "terminal", "capability": "Run shell commands", ...})
    record = store.get("terminal")
    found = store.query("shell")
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Required fields in every tool-memory record.
_REQUIRED_FIELDS = frozenset({"tool_name", "capability"})


def _store_dir() -> Path:
    """Return the tool-memory directory under HERMES_HOME."""
    from hermes_constants import get_hermes_home

    d = get_hermes_home() / "evolution" / "tool-memory"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _store_path() -> Path:
    """Return the JSON store file path."""
    return _store_dir() / "tools.json"


class ToolMemoryStore:
    """Persistent JSON-backed store for tool capability/failure records.

    The store is a dict keyed by ``tool_name``.  Each value is a record
    dict with the schema documented in the module docstring.
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path or _store_path()

    def _load(self) -> Dict[str, Dict[str, Any]]:
        """Load the store from disk, returning an empty dict if missing."""
        if not self._path.exists():
            return {}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to load tool-memory store %s: %s", self._path, exc)
        return {}

    def _save(self, data: Dict[str, Dict[str, Any]]) -> None:
        """Persist the store to disk."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("Failed to save tool-memory store %s: %s", self._path, exc)

    def put(self, record: Dict[str, Any]) -> None:
        """Insert or update a tool-memory record.

        Args:
            record: Must contain at least ``tool_name`` and ``capability``.
                    ``last_verified`` is auto-stamped if missing.

        Raises:
            ValueError: If required fields are missing.
        """
        missing = _REQUIRED_FIELDS - set(record)
        if missing:
            raise ValueError(f"Missing required fields: {missing}")
        data = self._load()
        tool_name = record["tool_name"]
        # Merge with existing record so callers can update partial fields.
        existing = data.get(tool_name, {})
        existing.update(record)
        if "last_verified" not in existing:
            existing["last_verified"] = datetime.now(timezone.utc).isoformat()
        data[tool_name] = existing
        self._save(data)

    def get(self, tool_name: str) -> Optional[Dict[str, Any]]:
        """Retrieve a record by tool name, or None if not found."""
        return self._load().get(tool_name)

    def query(self, keyword: str) -> List[Dict[str, Any]]:
        """Search records by keyword in capability or tool_name (case-insensitive).

        Args:
            keyword: Substring to search for.

        Returns:
            List of matching records (may be empty).
        """
        kw = keyword.lower()
        data = self._load()
        results = []
        for record in data.values():
            name = record.get("tool_name", "").lower()
            cap = record.get("capability", "").lower()
            if kw in name or kw in cap:
                results.append(record)
        return results

    def list_all(self) -> List[Dict[str, Any]]:
        """Return all records in the store."""
        return list(self._load().values())

    def remove(self, tool_name: str) -> bool:
        """Remove a record by tool name. Returns True if it existed."""
        data = self._load()
        if tool_name in data:
            del data[tool_name]
            self._save(data)
            return True
        return False

    def count(self) -> int:
        """Return the number of records in the store."""
        return len(self._load())
