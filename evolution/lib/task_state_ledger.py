# -*- coding: utf-8 -*-
"""Verified external task-state ledger (issue #2841).

Manage-execute-audit primitive: an append-only durable record of
``(step, verified outcome, evidence link)`` written on each completed stage and
read on resume/rewind, so a long-horizon stage re-anchors to *verified* progress
rather than recalled context.  Complements versioned_harness_state.py
(instruction rollback) and stage_cache.py (output caching): those persist
runnable state; this persists a traceable verdict that a step passed its audit.

Pure dataclasses, no external deps, import-safe, JSON round-trip, explicit
``encoding`` (ruff PLW1514).
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

__all__ = ["LedgerEntry", "TaskStateLedger", "get_default_ledger_dir"]


def get_default_ledger_dir() -> Path:
    """Default ledger storage dir (under HERMES_HOME)."""
    base = os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
    p = Path(base) / "task_state_ledger"
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return p


@dataclass
class LedgerEntry:
    """One verified completion. ``verified`` = externally audited (gate/CI);
    ``evidence`` = audit links substantiating the outcome."""

    step: str
    verified: bool = True
    outcome: str = ""
    evidence: List[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "LedgerEntry":
        return cls(
            step=str(data.get("step", "")),
            verified=bool(data.get("verified", True)),
            outcome=str(data.get("outcome", "")),
            evidence=list(data.get("evidence", [])),
            timestamp=float(data.get("timestamp", time.time())),
        )


class TaskStateLedger:
    """Append-only durable record of verified completed steps for one task."""

    def __init__(
        self,
        task_id: str,
        storage_dir: Optional[Union[str, Path]] = None,
        auto_save: bool = True,
    ) -> None:
        self.task_id = task_id
        self.storage_dir = (
            Path(storage_dir) if storage_dir else get_default_ledger_dir()
        )
        self.auto_save = auto_save
        self._entries: List[LedgerEntry] = []
        if self.storage_file.exists():
            self.load()

    @property
    def storage_file(self) -> Path:
        safe_id = "".join(
            c if c.isalnum() or c in ("-", "_") else "_" for c in self.task_id
        )
        return self.storage_dir / f"ledger_{safe_id}.json"

    @property
    def entries(self) -> List[LedgerEntry]:
        return list(self._entries)

    @property
    def last_verified_step(self) -> Optional[LedgerEntry]:
        return self._entries[-1] if self._entries else None

    def append(self, entry: LedgerEntry) -> LedgerEntry:
        """Record a verified completion; persist when auto_save."""
        self._entries.append(entry)
        if self.auto_save:
            self.save()
        return entry

    def completed(self, step: str) -> bool:
        """True if ``step`` already has a verified ledger entry."""
        return any(e.step == step for e in self._entries)

    def summary(self) -> str:
        """Compact resume anchor, one line per verified step."""
        if not self._entries:
            return "no verified steps recorded"
        return "\n".join(
            f"- {e.step}: {'verified' if e.verified else 'unverified'} — "
            f"{e.outcome or '(done)'} [{', '.join(e.evidence) or 'no evidence'}]"
            for e in self._entries
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "entries": [e.to_dict() for e in self._entries],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TaskStateLedger":
        ledger = cls(task_id=str(data.get("task_id", "default")), auto_save=False)
        entries = data.get("entries", [])
        if isinstance(entries, list):
            ledger._entries = [
                LedgerEntry.from_dict(e) for e in entries if isinstance(e, dict)
            ]
        return ledger

    def save(self, path: Optional[Union[str, Path]] = None) -> Path:
        """Atomically persist to disk (tmp + replace)."""
        target = Path(path) if path else self.storage_file
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(self.to_dict(), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            tmp.replace(target)
        except OSError:
            pass
        return target

    def load(self, path: Optional[Union[str, Path]] = None) -> bool:
        """Load entries from disk; True on success."""
        target = Path(path) if path else self.storage_file
        if not target.exists():
            return False
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
            entries = data.get("entries", [])
            if isinstance(entries, list):
                self._entries = [
                    LedgerEntry.from_dict(e) for e in entries if isinstance(e, dict)
                ]
                return True
        except (OSError, ValueError):
            pass
        return False
