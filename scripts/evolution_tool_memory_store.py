#!/usr/bin/env python3
"""Tool-memory store for persistent tool capability data (issue #1218).

Increment 1 of 3 for #1218 (parent #1178 — ToolAtlas: provider-side
tool-memory graph). Stores tool capability descriptions, failure
boundaries, composition partners, and last-verified timestamps in a
JSON file under ``~/.hermes/evolution/tool-memory/``.

The module is callable standalone via CLI so it is not dead code:
    python -m scripts.evolution_tool_memory_store add --tool terminal \\
        --capability "Execute shell commands" \\
        --failure-boundary "No interactive prompts"
    python -m scripts.evolution_tool_memory_store list
    python -m scripts.evolution_tool_memory_store query --tool terminal

Increments 2 (probing) and 3 (wiring into tool_describe) will consume
the store programmatically.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


def _get_tool_memory_dir() -> Path:
    """Return the tool-memory directory under HERMES_HOME."""
    hermes_home = os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))
    d = Path(hermes_home) / "evolution" / "tool-memory"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _get_store_path() -> Path:
    return _get_tool_memory_dir() / "tools.json"


class ToolMemoryStore:
    """Persistent store for tool capability and failure-boundary data."""

    def __init__(self) -> None:
        self._path = _get_store_path()

    def _load(self) -> Dict[str, Dict[str, Any]]:
        if not self._path.exists():
            return {}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, OSError):
            return {}

    def _save(self, data: Dict[str, Dict[str, Any]]) -> None:
        self._path.write_text(
            json.dumps(data, indent=2, default=str),
            encoding="utf-8",
        )

    def add(
        self,
        tool_name: str,
        capability: str = "",
        failure_boundaries: Optional[List[str]] = None,
        composition_partners: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Add or update a tool-memory record."""
        data = self._load()
        existing = data.get(tool_name, {})
        record = {
            "tool": tool_name,
            "capability": capability or existing.get("capability", ""),
            "failure_boundaries": failure_boundaries
            or existing.get("failure_boundaries", []),
            "composition_partners": composition_partners
            or existing.get("composition_partners", []),
            "last_verified": time.time(),
        }
        data[tool_name] = record
        self._save(data)
        return record

    def get(self, tool_name: str) -> Optional[Dict[str, Any]]:
        """Retrieve a single tool record."""
        return self._load().get(tool_name)

    def list_all(self) -> List[Dict[str, Any]]:
        """List all tool records."""
        data = self._load()
        return sorted(data.values(), key=lambda r: r.get("tool", ""))

    def remove(self, tool_name: str) -> bool:
        """Remove a tool record. Returns True if it existed."""
        data = self._load()
        if tool_name in data:
            del data[tool_name]
            self._save(data)
            return True
        return False

    def query(self, capability_keyword: str = "") -> List[Dict[str, Any]]:
        """Query tools by capability keyword (case-insensitive)."""
        records = self.list_all()
        if not capability_keyword:
            return records
        kw = capability_keyword.lower()
        return [r for r in records if kw in (r.get("capability") or "").lower()]


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point for the tool-memory store."""
    parser = argparse.ArgumentParser(
        description="Manage persistent tool capability and failure-boundary data.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add", help="Add or update a tool record")
    p_add.add_argument("--tool", required=True, help="Tool name")
    p_add.add_argument("--capability", default="", help="Capability description")
    p_add.add_argument(
        "--failure-boundary",
        action="append",
        default=[],
        help="Failure boundary (repeatable)",
    )
    p_add.add_argument(
        "--composition-partner",
        action="append",
        default=[],
        help="Composition partner (repeatable)",
    )

    p_list = sub.add_parser("list", help="List all tool records")

    p_query = sub.add_parser("query", help="Query tools by capability keyword")
    p_query.add_argument("--tool", default="", help="Specific tool name to look up")
    p_query.add_argument(
        "--capability", default="", help="Capability keyword to search"
    )

    p_remove = sub.add_parser("remove", help="Remove a tool record")
    p_remove.add_argument("--tool", required=True, help="Tool name to remove")

    args = parser.parse_args(argv)
    store = ToolMemoryStore()

    if args.command == "add":
        record = store.add(
            args.tool,
            capability=args.capability,
            failure_boundaries=args.failure_boundary or None,
            composition_partners=args.composition_partner or None,
        )
        print(json.dumps(record, indent=2, default=str))
        return 0

    if args.command == "list":
        records = store.list_all()
        print(json.dumps(records, indent=2, default=str))
        return 0

    if args.command == "query":
        if args.tool:
            record = store.get(args.tool)
            if record:
                print(json.dumps(record, indent=2, default=str))
                return 0
            print(f"Tool '{args.tool}' not found in store.", file=sys.stderr)
            return 1
        records = store.query(capability_keyword=args.capability)
        print(json.dumps(records, indent=2, default=str))
        return 0

    if args.command == "remove":
        if store.remove(args.tool):
            print(f"Removed '{args.tool}'.")
            return 0
        print(f"Tool '{args.tool}' not found.", file=sys.stderr)
        return 1

    return 1


if __name__ == "__main__":
    sys.exit(main())
