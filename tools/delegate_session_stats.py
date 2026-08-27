"""Per-session success-rate counters for delegate_task (#3225 slice 1).

A small observability hook that records completed vs failed child dispatches
keyed by the parent agent's durable ``session_id``. It does NOT persist across
process restarts and does NOT make decisions — it only exposes counts so
downstream logic (e.g. the #3224 loop-guard) can act on them.

Thread-safe: delegate_task can be invoked from tool-worker threads, so all
mutation happens under a lock.
"""

from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional


class _DelegateSessionStats:
    """In-memory per-session counters for delegate_task outcomes."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: Dict[str, Dict[str, int]] = {}

    def record(
        self, session_id: Optional[str], results: List[Dict[str, Any]]
    ) -> Optional[Dict[str, int]]:
        """Accumulate completed/failed counts for one dispatch batch.

        ``results`` is the per-task results array from a delegate_task return.
        A task counts as completed when its entry has ``status == "completed"``
        (or carries no error); anything else counts as failed. Returns the
        updated bucket for the session, or ``None`` when no session id is given.
        """
        if not session_id:
            return None
        total = len(results)
        completed = sum(
            1 for r in results if isinstance(r, dict) and r.get("status") == "completed"
        )
        failed = total - completed
        with self._lock:
            bucket = self._sessions.setdefault(
                session_id, {"dispatches": 0, "completed": 0, "failed": 0}
            )
            bucket["dispatches"] += total
            bucket["completed"] += completed
            bucket["failed"] += failed
            return dict(bucket)

    def get(self, session_id: Optional[str]) -> Optional[Dict[str, int]]:
        """Return a snapshot of the session's counters, or ``None`` if absent."""
        if not session_id:
            return None
        with self._lock:
            bucket = self._sessions.get(session_id)
            return dict(bucket) if bucket is not None else None

    def reset(self, session_id: Optional[str]) -> bool:
        """Drop the session's counters. Returns ``True`` if they existed."""
        if not session_id:
            return False
        with self._lock:
            return self._sessions.pop(session_id, None) is not None


DELEGATE_SESSION_STATS = _DelegateSessionStats()
