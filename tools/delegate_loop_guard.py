"""Consecutive-failure loop-guard for delegate_task (#3224 slice 2).

Mirrors the tool spiral guard (tools/terminal_tool.py, tools/file_tools.py)
for delegation: after N consecutive delegate_task failures with near-identical
goal signatures, forces a strategy change (different toolset, direct
execution, or explicit fallback) instead of re-dispatching the same shape.

Thread-safe: delegate_task dispatches can happen across threads, so all
internal counter state is guarded by a lock.
"""

from __future__ import annotations

import hashlib
import re
import threading
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_DELEGATE_MAX_LOOP_FAILURES = 3
_DELEGATE_MAX_LOOP_FAILURES_CEILING = 20
_delegate_max_loop_failures_cached: Optional[int] = None


def _get_delegate_max_loop_failures() -> int:
    """Return the configured delegate loop-guard failure budget."""
    global _delegate_max_loop_failures_cached
    if _delegate_max_loop_failures_cached is not None:
        return _delegate_max_loop_failures_cached
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
        val = cfg.get("delegate", {}).get(
            "max_loop_failures", DEFAULT_DELEGATE_MAX_LOOP_FAILURES
        )
        if isinstance(val, int) and val > 0:
            _delegate_max_loop_failures_cached = min(
                val, _DELEGATE_MAX_LOOP_FAILURES_CEILING
            )
            return _delegate_max_loop_failures_cached
    except Exception:
        pass
    _delegate_max_loop_failures_cached = DEFAULT_DELEGATE_MAX_LOOP_FAILURES
    return _delegate_max_loop_failures_cached


def _compute_goal_signature(tasks: List[Dict[str, Any]]) -> str:
    """Compute a compact, normalized hash for a batch of delegation tasks."""
    normalized_parts = []
    for t in tasks:
        if isinstance(t, dict):
            task_str = str(t.get("task") or t.get("goal") or t.get("description") or "")
        else:
            task_str = str(t)
        # Normalize whitespace and case
        cleaned = re.sub(r"\s+", " ", task_str.strip().lower())
        normalized_parts.append(cleaned)

    h = hashlib.sha1()
    for part in normalized_parts:
        h.update(part.encode("utf-8", "replace"))
        h.update(b"\x00")
    return h.hexdigest()


def _is_task_failure(result: Dict[str, Any]) -> bool:
    """Check if an individual child task result represents a failure."""
    if not isinstance(result, dict):
        return True
    status = result.get("status")
    if status == "completed":
        return False
    if status == "failed" or status == "error":
        return True
    if result.get("error") is not None:
        return True
    return False


class _DelegateLoopGuardTracker:
    """Thread-safe per-session tracker for consecutive delegate_task failures."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: Dict[str, Tuple[str, int]] = {}

    def record_and_evaluate(
        self,
        session_id: Optional[str],
        tasks: List[Dict[str, Any]],
        results: List[Dict[str, Any]],
        budget: Optional[int] = None,
    ) -> Tuple[bool, int, Optional[str]]:
        """Record batch outcomes and evaluate if loop guard is tripped.

        Returns (tripped: bool, consecutive_failures: int, diagnostic: Optional[str]).
        """
        if not session_id:
            return False, 0, None

        has_success = any(not _is_task_failure(r) for r in results) if results else False

        with self._lock:
            if has_success:
                # Any successful child in the batch resets the consecutive failure spiral
                self._sessions.pop(session_id, None)
                return False, 0, None

            sig = _compute_goal_signature(tasks)
            last_sig, count = self._sessions.get(session_id, ("", 0))
            count = count + 1 if sig == last_sig else 1
            self._sessions[session_id] = (sig, count)

            effective_budget = budget or _get_delegate_max_loop_failures()
            tripped = count >= effective_budget
            diagnostic = None
            if tripped:
                diagnostic = (
                    f"Delegate loop guard tripped: identical delegation goal has failed "
                    f"{count} consecutive times in this session (budget={effective_budget}). "
                    f"Do NOT retry identical delegate_task dispatches. Change strategy: "
                    f"execute tasks directly in the primary session, modify subagent "
                    f"instructions/toolsets, or report blocker to user."
                )
            return tripped, count, diagnostic

    def get_consecutive_failures(self, session_id: Optional[str]) -> int:
        """Return the current consecutive identical failure count for session."""
        if not session_id:
            return 0
        with self._lock:
            return self._sessions.get(session_id, ("", 0))[1]

    def reset(self, session_id: Optional[str]) -> bool:
        """Clear state for the given session."""
        if not session_id:
            return False
        with self._lock:
            return self._sessions.pop(session_id, None) is not None


DELEGATE_LOOP_GUARD = _DelegateLoopGuardTracker()
