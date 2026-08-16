# -*- coding: utf-8 -*-
"""Persistent programmatic context — context-as-variable store (Issue #2496).

Enables working context and tool results to persist as named, re-readable objects
across turns and compaction events, allowing indexing, slicing, and re-summarizing
without re-reading raw transcript text.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

logger = logging.getLogger(__name__)


def get_default_context_store_dir() -> Path:
    """Resolve default directory for programmatic context variable stores."""
    base = os.environ.get("HERMES_HOME")
    if base:
        p = Path(base) / "context_vars"
    else:
        p = Path.home() / ".hermes" / "context_vars"
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return p


@dataclass
class ContextVariable:
    """Metadata and content for a single named context variable."""

    name: str
    value: Any
    description: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> ContextVariable:
        return cls(
            name=str(data.get("name", "")),
            value=data.get("value"),
            description=str(data.get("description", "")),
            created_at=float(data.get("created_at", time.time())),
            updated_at=float(data.get("updated_at", time.time())),
        )


class ProgrammaticContextStore:
    """Store for named variables that outlive turns and compaction events."""

    def __init__(
        self,
        session_id: Optional[str] = None,
        storage_dir: Optional[Union[str, Path]] = None,
        auto_save: bool = True,
    ) -> None:
        self.session_id = session_id or "default"
        self.storage_dir = (
            Path(storage_dir) if storage_dir else get_default_context_store_dir()
        )
        self.auto_save = auto_save
        self._variables: Dict[str, ContextVariable] = {}
        if self.storage_file.exists():
            self.load()

    @property
    def storage_file(self) -> Path:
        """Path to JSON file storing variables for this session."""
        safe_id = "".join(
            c if c.isalnum() or c in ("-", "_") else "_" for c in self.session_id
        )
        return self.storage_dir / f"vars_{safe_id}.json"

    def set(self, name: str, value: Any, description: str = "") -> None:
        """Store or update a named context variable."""
        now = time.time()
        if name in self._variables:
            var = self._variables[name]
            var.value = value
            if description:
                var.description = description
            var.updated_at = now
        else:
            self._variables[name] = ContextVariable(
                name=name,
                value=value,
                description=description,
                created_at=now,
                updated_at=now,
            )
        if self.auto_save:
            self.save()

    def get(self, name: str, default: Optional[Any] = None) -> Any:
        """Retrieve the value of a named context variable."""
        var = self._variables.get(name)
        if var is not None:
            return var.value
        return default

    def slice(
        self, name: str, start: Optional[int] = None, end: Optional[int] = None
    ) -> Optional[Any]:
        """Slice a string or sequence variable by indices without loading entire transcript."""
        val = self.get(name)
        if val is None:
            return None
        if isinstance(val, (str, list, tuple, bytes)):
            return val[start:end]
        return val

    def delete(self, name: str) -> bool:
        """Delete a named variable from the store."""
        if name in self._variables:
            del self._variables[name]
            if self.auto_save:
                self.save()
            return True
        return False

    def list_vars(self) -> List[Dict[str, Any]]:
        """List summary metadata for all stored context variables."""
        results = []
        for var in self._variables.values():
            val = var.value
            val_type = type(val).__name__
            length = len(val) if hasattr(val, "__len__") else 1
            results.append({
                "name": var.name,
                "type": val_type,
                "length": length,
                "description": var.description,
                "updated_at": var.updated_at,
            })
        return results

    def clear(self) -> None:
        """Clear all stored variables."""
        self._variables.clear()
        if self.auto_save and self.storage_file.exists():
            try:
                self.storage_file.unlink()
            except OSError:
                pass

    def summarize(self) -> str:
        """Generate a concise index table of variables for prompts and summaries."""
        if not self._variables:
            return "No programmatic context variables stored."
        lines = [
            "# Programmatic Context Variables",
            "| Variable | Type | Length | Description |",
            "|---|---|---|---|",
        ]
        for var in self._variables.values():
            val = var.value
            t = type(val).__name__
            l = len(val) if hasattr(val, "__len__") else 1
            d = var.description or "-"
            lines.append(f"| `{var.name}` | {t} | {l} | {d} |")
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize store to dictionary."""
        return {
            "session_id": self.session_id,
            "variables": {k: v.to_dict() for k, v in self._variables.items()},
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> ProgrammaticContextStore:
        """Construct store from dictionary."""
        store = cls(session_id=data.get("session_id", "default"), auto_save=False)
        vars_data = data.get("variables", {})
        if isinstance(vars_data, dict):
            for k, v in vars_data.items():
                if isinstance(v, dict):
                    store._variables[k] = ContextVariable.from_dict(v)
        return store

    def save(self, path: Optional[Union[str, Path]] = None) -> Path:
        """Persist store variables to disk."""
        target = Path(path) if path else self.storage_file
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        except OSError as e:
            logger.warning("Failed to save programmatic context store: %s", e)
        return target

    def load(self, path: Optional[Union[str, Path]] = None) -> bool:
        """Load store variables from disk."""
        target = Path(path) if path else self.storage_file
        if not target.exists():
            return False
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
            vars_data = data.get("variables", {})
            if isinstance(vars_data, dict):
                self._variables = {
                    k: ContextVariable.from_dict(v)
                    for k, v in vars_data.items()
                    if isinstance(v, dict)
                }
            return True
        except (OSError, ValueError) as e:
            logger.warning("Failed to load programmatic context store: %s", e)
            return False

    def execute_command(self, action: str, *args: str) -> str:
        """Execute a CLI action (set, get, slice, list, delete, summarize)."""
        act = (action or "").strip().lower()
        if act == "set" and len(args) >= 2:
            name, val = args[0], " ".join(args[1:])
            desc = ""
            self.set(name, val, description=desc)
            return f"Variable '{name}' set ({len(val)} chars)."
        elif act == "get" and len(args) >= 1:
            val = self.get(args[0])
            return str(val) if val is not None else f"Variable '{args[0]}' not found."
        elif act == "slice" and len(args) >= 2:
            name = args[0]
            slice_spec = args[1]
            try:
                parts = slice_spec.split(":")
                s = int(parts[0]) if parts[0] else None
                e = int(parts[1]) if len(parts) > 1 and parts[1] else None
                res = self.slice(name, s, e)
                return str(res) if res is not None else f"Variable '{name}' not found."
            except ValueError:
                return "Invalid slice spec (expected start:end)."
        elif act in ("list", "ls"):
            vars_list = self.list_vars()
            return json.dumps(vars_list, indent=2)
        elif act in ("del", "delete", "rm") and len(args) >= 1:
            ok = self.delete(args[0])
            return (
                f"Variable '{args[0]}' deleted."
                if ok
                else f"Variable '{args[0]}' not found."
            )
        elif act in ("summary", "summarize"):
            return self.summarize()
        else:
            return "Usage: context-var {set|get|slice|list|delete|summary} [args...]"
