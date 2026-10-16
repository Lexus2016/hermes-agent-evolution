"""Mutating-step safety: snapshot-before-destructive primitive + failure-cause counter (#3014, #2995).

Implements the SABER / Do-over mutating-step safety pattern:
1. FileSnapshot: snapshot target files before executing a destructive or
   mutating command, with automatic or explicit rollback capabilities.
2. MutatingFailureCounter: record how often a failed run's root cause was a
   mutating step and surface the share for realized-impact metrics.

Pure standard library only, thread-safe, no side effects on import.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union
from datetime import UTC

__version__ = "1.1.0"


@dataclass
class FailureCauseSummary:
    """Aggregate of recorded failure causes.

    Attributes:
        failed_runs: Total number of failed runs recorded.
        mutating_cause: Count whose root cause was a mutating step.
        other_cause: Count whose root cause was NOT a mutating step.
        mutating_share: Fraction of failed runs caused by a mutating step
            (``None`` when no failures have been recorded).
    """

    failed_runs: int = 0
    mutating_cause: int = 0
    other_cause: int = 0
    mutating_share: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dict (for realized-impact metrics)."""
        return {
            "failed_runs": self.failed_runs,
            "mutating_cause": self.mutating_cause,
            "other_cause": self.other_cause,
            "mutating_share": self.mutating_share,
        }


class MutatingFailureCounter:
    """Counts failed runs whose root cause was a mutating step.

    Appends one JSONL record per failed run and reports the share of failed
    runs whose root cause was a mutating step — the metric #2995 wants surfaced
    in realized-impact metrics.

    Ledger schema: ``{"run_id", "outcome": "failed", "cause_category":
    "mutating"|"other", "recorded_at": "YYYY-MM-DD"}``. Only failed runs are
    recorded.
    """

    def __init__(self, ledger_file: Optional[os.PathLike | str] = None) -> None:
        """Initialize with an optional ledger path."""
        if ledger_file is None:
            base = Path(
                os.environ.get(
                    "EVOLUTION_PROFILE_DIR", str(Path.home() / ".hermes" / "evolution")
                )
            )
            ledger_file = base / "mutation_guard" / "failure-causes.jsonl"
        self._ledger = Path(ledger_file)
        self._lock = threading.Lock()

    def record(
        self,
        run_id: str,
        cause_category: str,
        recorded_at: Optional[str] = None,
    ) -> None:
        """Record a failed run's root-cause category (mutating vs other)."""
        if cause_category not in ("mutating", "other"):
            raise ValueError("cause_category must be 'mutating' or 'other'")
        if not recorded_at:
            from datetime import datetime, timezone

            recorded_at = datetime.now(UTC).date().isoformat()
        rec = {
            "run_id": str(run_id),
            "outcome": "failed",
            "cause_category": cause_category,
            "recorded_at": recorded_at,
        }
        with self._lock:
            self._ledger.parent.mkdir(parents=True, exist_ok=True)
            with open(self._ledger, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, sort_keys=True) + "\n")

    def summary(self) -> FailureCauseSummary:
        """Aggregate the ledger; malformed lines are skipped."""
        mutating = 0
        total = 0
        if self._ledger.exists():
            for ln in self._ledger.read_text(encoding="utf-8").splitlines():
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    rec = json.loads(ln)
                except (json.JSONDecodeError, ValueError):
                    continue
                if rec.get("outcome") != "failed":
                    continue
                total += 1
                if rec.get("cause_category") == "mutating":
                    mutating += 1
        share = round(mutating / total, 3) if total else None
        return FailureCauseSummary(
            failed_runs=total,
            mutating_cause=mutating,
            other_cause=total - mutating,
            mutating_share=share,
        )


class FileSnapshot:
    """Snapshot-before-destructive-command primitive (SABER / Do-over pattern).

    Captures file states before a mutating command runs, enabling reliable
    rollback on failure. Supports both explicit rollback via :meth:`restore`
    and context-manager rollback on unhandled exceptions.
    """

    def __init__(self, paths: Sequence[os.PathLike | str]) -> None:
        """Snapshot the given paths (files that exist or might be created)."""
        self._snapshots: dict[Path, Optional[bytes]] = {}
        for p in paths:
            path = Path(p).resolve()
            if path.is_file():
                self._snapshots[path] = path.read_bytes()
            else:
                self._snapshots[path] = None

    @property
    def paths(self) -> list[Path]:
        """List of snapshotted file paths."""
        return list(self._snapshots.keys())

    def has_changed(self) -> bool:
        """Check if any snapshotted path has diverged from snapshot time."""
        for path, orig_content in self._snapshots.items():
            if orig_content is None:
                if path.exists():
                    return True
            else:
                if not path.is_file() or path.read_bytes() != orig_content:
                    return True
        return False

    def restore(self) -> int:
        """Restore all snapshotted files to their state at snapshot time.

        - If the file existed originally, restores original bytes.
        - If the file was created after snapshot time, deletes it.

        Returns the number of files modified or deleted during restoration.
        """
        restored_count = 0
        for path, orig_content in self._snapshots.items():
            if orig_content is None:
                if path.exists():
                    if path.is_file() or path.is_symlink():
                        path.unlink()
                        restored_count += 1
            else:
                current = path.read_bytes() if path.is_file() else None
                if current != orig_content:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(orig_content)
                    restored_count += 1
        return restored_count

    def __enter__(self) -> FileSnapshot:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: Optional[BaseException],
        exc_tb: object,
    ) -> bool:
        if exc_type is not None:
            self.restore()
        return False  # Do not suppress the exception

