# -*- coding: utf-8 -*-
"""Self-refining harness state with versioned rollback (Issue #2497, Slice B).

Enables the agent to refine its operating instructions mid-task while keeping
a structured, versioned history with safe rollback capabilities.
"""

from __future__ import annotations

import difflib
import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

logger = logging.getLogger(__name__)


def get_default_harness_state_dir() -> Path:
    """Resolve default directory for harness state version storage."""
    base = os.environ.get("HERMES_HOME")
    if base:
        p = Path(base) / "harness_states"
    else:
        p = Path.home() / ".hermes" / "harness_states"
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return p


@dataclass
class HarnessVersion:
    """A snapshot of operating instructions at a specific version."""

    version: int
    instructions: str
    reason: str = ""
    author: str = "agent"
    timestamp: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> HarnessVersion:
        return cls(
            version=int(data.get("version", 1)),
            instructions=str(data.get("instructions", "")),
            reason=str(data.get("reason", "")),
            author=str(data.get("author", "agent")),
            timestamp=float(data.get("timestamp", time.time())),
            metadata=dict(data.get("metadata", {})),
        )


class VersionedHarnessState:
    """Harness state manager tracking version history and enabling rollbacks."""

    def __init__(
        self,
        initial_instructions: str = "",
        session_id: Optional[str] = None,
        storage_dir: Optional[Union[str, Path]] = None,
        auto_save: bool = True,
    ) -> None:
        self.session_id = session_id or "default"
        self.storage_dir = (
            Path(storage_dir) if storage_dir else get_default_harness_state_dir()
        )
        self.auto_save = auto_save
        self._history: List[HarnessVersion] = []
        if self.storage_file.exists():
            if not self.load():
                self._init_first_version(initial_instructions)
        else:
            self._init_first_version(initial_instructions)

    def _init_first_version(self, instructions: str) -> None:
        v1 = HarnessVersion(
            version=1,
            instructions=instructions,
            reason="Initial harness instructions",
            author="system",
            timestamp=time.time(),
        )
        self._history = [v1]
        if self.auto_save:
            self.save()

    @property
    def storage_file(self) -> Path:
        """Path to JSON file storing harness history for this session."""
        safe_id = "".join(
            c if c.isalnum() or c in ("-", "_") else "_" for c in self.session_id
        )
        return self.storage_dir / f"harness_{safe_id}.json"

    @property
    def current_version(self) -> int:
        """Current latest version number."""
        return self._history[-1].version if self._history else 1

    @property
    def current_instructions(self) -> str:
        """Current operating instructions."""
        return self._history[-1].instructions if self._history else ""

    @property
    def history(self) -> List[HarnessVersion]:
        """Full list of version snapshots."""
        return list(self._history)

    def update(
        self,
        instructions: str,
        reason: str = "",
        author: str = "agent",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> HarnessVersion:
        """Update instructions and commit a new version to history."""
        next_v = self.current_version + 1
        ver = HarnessVersion(
            version=next_v,
            instructions=instructions,
            reason=reason or f"Update to version {next_v}",
            author=author,
            timestamp=time.time(),
            metadata=metadata or {},
        )
        self._history.append(ver)
        if self.auto_save:
            self.save()
        return ver

    def rollback(
        self, target_version: Optional[int] = None, reason: str = ""
    ) -> HarnessVersion:
        """Roll back instructions to a previous version."""
        if len(self._history) <= 1 and (
            target_version is None or target_version == self.current_version
        ):
            raise ValueError("Cannot rollback: no prior versions exist.")

        if target_version is None:
            target_version = self.current_version - 1

        target_snap = self.get_version(target_version)
        if target_snap is None:
            raise ValueError(
                f"Target version {target_version} does not exist in history."
            )

        rb_reason = (
            f"Rollback to v{target_version}: {reason}"
            if reason
            else f"Rollback to v{target_version} (was v{self.current_version})"
        )
        return self.update(
            instructions=target_snap.instructions,
            reason=rb_reason,
            author="system",
            metadata={
                "rollback_from": self.current_version,
                "rollback_target": target_version,
            },
        )

    def get_version(self, version: int) -> Optional[HarnessVersion]:
        """Retrieve a specific version snapshot."""
        for v in self._history:
            if v.version == version:
                return v
        return None

    def diff(self, v1: int, v2: int) -> str:
        """Generate unified diff between two versions."""
        snap1 = self.get_version(v1)
        snap2 = self.get_version(v2)
        if snap1 is None or snap2 is None:
            return f"Error: one or both versions ({v1}, {v2}) not found."
        lines1 = snap1.instructions.splitlines(keepends=True)
        lines2 = snap2.instructions.splitlines(keepends=True)
        diff_lines = difflib.unified_diff(
            lines1,
            lines2,
            fromfile=f"v{v1} ({snap1.reason})",
            tofile=f"v{v2} ({snap2.reason})",
        )
        return "".join(diff_lines) or "No changes between versions."

    def list_versions(self) -> List[Dict[str, Any]]:
        """List metadata summary for all versions."""
        return [
            {
                "version": v.version,
                "reason": v.reason,
                "author": v.author,
                "length": len(v.instructions),
                "timestamp": v.timestamp,
            }
            for v in self._history
        ]

    def to_dict(self) -> Dict[str, Any]:
        """Serialize state and history to dictionary."""
        return {
            "session_id": self.session_id,
            "history": [v.to_dict() for v in self._history],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> VersionedHarnessState:
        """Construct state from dictionary."""
        state = cls(session_id=data.get("session_id", "default"), auto_save=False)
        hist_data = data.get("history", [])
        if isinstance(hist_data, list) and hist_data:
            state._history = [
                HarnessVersion.from_dict(v) for v in hist_data if isinstance(v, dict)
            ]
        return state

    def save(self, path: Optional[Union[str, Path]] = None) -> Path:
        """Save history to disk."""
        target = Path(path) if path else self.storage_file
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        except OSError as e:
            logger.warning("Failed to save harness state history: %s", e)
        return target

    def load(self, path: Optional[Union[str, Path]] = None) -> bool:
        """Load history from disk."""
        target = Path(path) if path else self.storage_file
        if not target.exists():
            return False
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
            hist_data = data.get("history", [])
            if isinstance(hist_data, list) and hist_data:
                self._history = [
                    HarnessVersion.from_dict(v)
                    for v in hist_data
                    if isinstance(v, dict)
                ]
                return True
        except (OSError, ValueError) as e:
            logger.warning("Failed to load harness state history: %s", e)
        return False

    def execute_command(self, action: str, *args: str) -> str:
        """Execute a harness management CLI action."""
        act = (action or "").strip().lower()
        if act == "update" and args:
            instr = args[0]
            reason = args[1] if len(args) > 1 else ""
            ver = self.update(instr, reason=reason)
            return f"Updated harness to v{ver.version}: {ver.reason}"
        elif act in ("rollback", "revert"):
            target = int(args[0]) if args and args[0].isdigit() else None
            reason = args[1] if len(args) > 1 else ""
            try:
                ver = self.rollback(target_version=target, reason=reason)
                return f"Rolled back harness to v{ver.version}: {ver.reason}"
            except ValueError as e:
                return f"Rollback failed: {e}"
        elif act in ("list", "history"):
            return json.dumps(self.list_versions(), indent=2)
        elif act == "current":
            return self.current_instructions
        elif (
            act == "diff" and len(args) >= 2 and args[0].isdigit() and args[1].isdigit()
        ):
            return self.diff(int(args[0]), int(args[1]))
        elif act == "show" and args and args[0].isdigit():
            snap = self.get_version(int(args[0]))
            return snap.instructions if snap else f"Version {args[0]} not found."
        else:
            return "Usage: harness {update|rollback|list|current|diff|show} [args...]"
