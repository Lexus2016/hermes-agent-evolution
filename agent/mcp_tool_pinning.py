"""MCP tool definition pinning — fingerprint verification for MCP tools (#944).

Computes and stores a hash of each MCP server's tool definitions (name +
description + input_schema) at connection time. On subsequent connections,
compares current tool definitions against pinned fingerprints. When
definitions change, logs a WARNING with the specific changes and returns
a structured diff so the caller can decide whether to proceed.

This protects against:
- Tool poisoning: malicious instructions injected into tool descriptions
- Rug-pull attacks: tool behavior changed after initial trust is established
- Silent tool addition: new tools added without user awareness

Pins are stored per-profile at ``~/.hermes/mcp_pins/<server_name>.json``
via ``get_hermes_home()`` for profile safety.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)


@dataclass
class ToolPinDiff:
    """Diff between pinned and current MCP tool definitions."""

    added: List[str] = field(default_factory=list)
    removed: List[str] = field(default_factory=list)
    modified: List[str] = field(default_factory=list)
    unchanged: List[str] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(self.added or self.removed or self.modified)

    def summary(self) -> str:
        """Human-readable summary of changes."""
        parts: List[str] = []
        if self.added:
            parts.append(f"added: {', '.join(sorted(self.added))}")
        if self.removed:
            parts.append(f"removed: {', '.join(sorted(self.removed))}")
        if self.modified:
            parts.append(f"modified: {', '.join(sorted(self.modified))}")
        return "; ".join(parts) if parts else "no changes"


def _normalize_schema(schema: Optional[dict]) -> Any:
    """Normalize an MCP tool input schema for stable hashing.

    Sorts dict keys recursively so key-order differences don't produce
    false-positive modification flags.
    """
    if schema is None:
        return None
    if isinstance(schema, dict):
        return {k: _normalize_schema(v) for k, v in sorted(schema.items())}
    if isinstance(schema, list):
        return [_normalize_schema(item) for item in schema]
    return schema


def compute_tool_fingerprint(
    tool_name: str,
    description: str,
    input_schema: Optional[dict],
) -> str:
    """Compute a SHA-256 fingerprint for a single MCP tool definition.

    The fingerprint covers the tool's name, description, and input schema.
    Any change to any of these fields produces a different hash, enabling
    change detection on reconnect.
    """
    canonical = json.dumps(
        {
            "name": tool_name,
            "description": description or "",
            "input_schema": _normalize_schema(input_schema),
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_server_fingerprint(tools: List[Any]) -> Dict[str, str]:
    """Compute fingerprints for all tools from an MCP server.

    Args:
        tools: List of MCP Tool objects (or dicts) with .name, .description,
               and .inputSchema/.input_schema attributes/keys.

    Returns:
        Dict mapping tool name → SHA-256 fingerprint.
    """
    fingerprints: Dict[str, str] = {}
    for tool in tools:
        name = getattr(tool, "name", None) or (tool.get("name") if isinstance(tool, dict) else None)
        if not name:
            continue
        description = (
            getattr(tool, "description", None)
            or (tool.get("description") if isinstance(tool, dict) else None)
            or ""
        )
        input_schema = (
            getattr(tool, "inputSchema", None)
            or getattr(tool, "input_schema", None)
            or (tool.get("inputSchema") if isinstance(tool, dict) else None)
            or (tool.get("input_schema") if isinstance(tool, dict) else None)
        )
        fingerprints[name] = compute_tool_fingerprint(name, description, input_schema)
    return fingerprints


def _pins_dir() -> Path:
    """Return the per-profile MCP pins directory, creating it if needed."""
    pins_dir = get_hermes_home() / "mcp_pins"
    pins_dir.mkdir(parents=True, exist_ok=True)
    return pins_dir


def _pins_path(server_name: str) -> Path:
    """Return the pin file path for a given MCP server name."""
    # Sanitize server name for filesystem safety
    safe_name = "".join(c if c.isalnum() or c in "-_." else "_" for c in server_name)
    return _pins_dir() / f"{safe_name}.json"


def load_pins(server_name: str) -> Optional[Dict[str, str]]:
    """Load pinned tool fingerprints for a server.

    Returns None if no pins exist (first connection).
    """
    path = _pins_path(server_name)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "tools" in data:
            return data["tools"]
        return None
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to load MCP pins for '%s': %s", server_name, exc)
        return None


def save_pins(server_name: str, fingerprints: Dict[str, str]) -> None:
    """Persist tool fingerprints as the pinned state for a server."""
    path = _pins_path(server_name)
    try:
        data = {
            "server": server_name,
            "tools": fingerprints,
        }
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        logger.warning("Failed to save MCP pins for '%s': %s", server_name, exc)


def diff_pins(
    pinned: Dict[str, str],
    current: Dict[str, str],
) -> ToolPinDiff:
    """Compare pinned fingerprints against current ones.

    Returns a ToolPinDiff describing added, removed, modified, and
    unchanged tools.
    """
    pinned_names: Set[str] = set(pinned.keys())
    current_names: Set[str] = set(current.keys())

    added = sorted(current_names - pinned_names)
    removed = sorted(pinned_names - current_names)
    modified = sorted(
        name for name in (pinned_names & current_names) if pinned[name] != current[name]
    )
    unchanged = sorted(
        name for name in (pinned_names & current_names) if pinned[name] == current[name]
    )

    return ToolPinDiff(added=added, removed=removed, modified=modified, unchanged=unchanged)


def verify_tools(
    server_name: str,
    current_tools: List[Any],
) -> ToolPinDiff:
    """Verify MCP tool definitions against pinned fingerprints.

    On first connection (no pins exist), saves current fingerprints and
    returns an empty diff. On subsequent connections, compares current
    tools against pins and returns the diff. If changes are detected,
    logs a WARNING with the details. The pins are NOT auto-updated —
    the caller decides whether to proceed (and optionally call
    ``save_pins`` to accept the new state).

    Args:
        server_name: The MCP server's logical name from config.
        current_tools: List of MCP Tool objects from ``list_tools()``.

    Returns:
        ToolPinDiff describing any changes from the pinned state.
    """
    current = compute_server_fingerprint(current_tools)
    pinned = load_pins(server_name)

    if pinned is None:
        # First connection — establish trust baseline.
        save_pins(server_name, current)
        logger.info(
            "MCP server '%s': pinned %d tool definition(s) as trust baseline",
            server_name,
            len(current),
        )
        return ToolPinDiff(unchanged=sorted(current.keys()))

    diff = diff_pins(pinned, current)

    if diff.has_changes:
        logger.warning(
            "MCP server '%s': tool definitions changed from pinned state — %s. "
            "Review these changes before trusting the updated tools.",
            server_name,
            diff.summary(),
        )
    else:
        logger.debug(
            "MCP server '%s': all %d tool definition(s) match pinned state",
            server_name,
            len(diff.unchanged),
        )

    return diff


def accept_new_pins(server_name: str, current_tools: List[Any]) -> None:
    """Accept current tool definitions as the new pinned state.

    Called when the user (or the caller) decides the changes are safe
    and wants to update the trust baseline.
    """
    current = compute_server_fingerprint(current_tools)
    save_pins(server_name, current)
    logger.info(
        "MCP server '%s': accepted new tool definitions as pin baseline (%d tools)",
        server_name,
        len(current),
    )


def sanitize_tool_description(description: str) -> str:
    """Sanitize an MCP tool description before presenting it to the model.

    Strips HTML comments, zero-width characters, and other patterns that
    could carry hidden prompt-injection payloads.
    """
    import re

    # Strip HTML comments (common injection vector)
    sanitized = re.sub(r"<!--.*?-->", "", description, flags=re.DOTALL)
    # Strip zero-width characters (invisible instruction smuggling)
    sanitized = re.sub(r"[\u200b\u200c\u200d\u200e\u200f\ufeff]", "", sanitized)
    # Strip other control characters except newlines and tabs
    sanitized = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", sanitized)
    return sanitized.strip()