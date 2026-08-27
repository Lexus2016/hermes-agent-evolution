# -*- coding: utf-8 -*-
"""Retry-spiral circuit breaker and diagnostic fallback for core tool invocations (#3241).

Prevents infinite retry spirals on core tools (read_file, patch, terminal, search_files)
by tracking consecutive failures per session, classifying the failure mode, and
tripping with structured diagnostic guidance once the per-tool budget is exceeded.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

DEFAULT_TOOL_RETRY_BUDGETS = {
    "read_file": 3,
    "write_file": 3,
    "patch": 3,
    "search_files": 3,
    "terminal": 5,
}

FAILURE_CLASS_NOT_FOUND = "not_found"
FAILURE_CLASS_PERMISSION = "permission_denied"
FAILURE_CLASS_INVALID_ARG = "invalid_argument"
FAILURE_CLASS_TIMEOUT = "timeout"
FAILURE_CLASS_PROVIDER = "provider_error"
FAILURE_CLASS_TRANSIENT = "transient"


def classify_tool_error(error_text: Optional[str]) -> str:
    """Deterministically classify a tool error string into a standard failure class."""
    if not error_text or not isinstance(error_text, str):
        return FAILURE_CLASS_TRANSIENT
    txt = error_text.lower()
    if any(s in txt for s in ("not found", "no such file", "enoent", "404", "cannot find")):
        return FAILURE_CLASS_NOT_FOUND
    if any(s in txt for s in ("permission denied", "access denied", "eacces", "403", "unauthorized", "operation not permitted")):
        return FAILURE_CLASS_PERMISSION
    if any(s in txt for s in ("invalid argument", "valueerror", "typeerror", "schema", "required argument", "unexpected keyword")):
        return FAILURE_CLASS_INVALID_ARG
    if any(s in txt for s in ("timeout", "timed out", "deadline exceeded", "econnreset")):
        return FAILURE_CLASS_TIMEOUT
    if any(s in txt for s in ("rate limit", "429", "500", "502", "503", "service unavailable", "quota")):
        return FAILURE_CLASS_PROVIDER
    return FAILURE_CLASS_TRANSIENT


def get_strategy_recommendation(tool_name: str, failure_class: str, last_error: Optional[str] = None) -> str:
    """Return targeted diagnostic recommendations for exhausted tool retry budgets."""
    t = (tool_name or "").lower()
    if t == "patch":
        return "Re-read the target file with read_file to inspect current line numbers and exact content before attempting another patch."
    if t == "read_file":
        return "Verify the target path exists and check permissions before reading again."
    if t == "search_files":
        return "Simplify search query or regex, or check the search directory path."
    if t == "terminal":
        return "For long-running or failing commands, verify prerequisites, arguments, or consider running as a background task."
    return "Inspect tool arguments and preconditions before retrying."


class _ToolCircuitBreakerTracker:
    """Thread-safe tracker for tool invocation success rates and retry spirals."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: Dict[str, Dict[str, Dict[str, Any]]] = {}

    def record_result(
        self,
        session_id: str,
        tool_name: str,
        status: str,
        error_message: Optional[str] = None,
        budget: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """Record a tool result and return a diagnostic event if the circuit breaker trips."""
        with self._lock:
            sid = str(session_id or "default").strip()
            tname = str(tool_name or "").strip()
            if not sid or not tname:
                return None

            sess_map = self._state.setdefault(sid, {})
            tool_rec = sess_map.setdefault(
                tname,
                {
                    "consecutive_failures": 0,
                    "total_calls": 0,
                    "total_failures": 0,
                    "last_error": None,
                    "last_failure_class": None,
                },
            )

            tool_rec["total_calls"] += 1

            if status == "error":
                tool_rec["consecutive_failures"] += 1
                tool_rec["total_failures"] += 1
                tool_rec["last_error"] = error_message
                fclass = classify_tool_error(error_message)
                tool_rec["last_failure_class"] = fclass

                eff_budget = budget or DEFAULT_TOOL_RETRY_BUDGETS.get(tname, 3)
                consecutive = tool_rec["consecutive_failures"]

                if consecutive >= eff_budget:
                    recommendation = get_strategy_recommendation(tname, fclass, error_message)
                    return {
                        "circuit_breaker_tripped": True,
                        "consecutive_failures": consecutive,
                        "budget": eff_budget,
                        "failure_class": fclass,
                        "tool_name": tname,
                        "last_error": error_message,
                        "strategy_recommendation": recommendation,
                    }
                return None
            else:
                tool_rec["consecutive_failures"] = 0
                return None

    def get_session_stats(self, session_id: str) -> Dict[str, Any]:
        with self._lock:
            sid = str(session_id or "default").strip()
            return {k: dict(v) for k, v in self._state.get(sid, {}).items()}

    def reset(self, session_id: Optional[str] = None) -> None:
        with self._lock:
            if session_id:
                self._state.pop(str(session_id).strip(), None)
            else:
                self._state.clear()


TOOL_CIRCUIT_BREAKER = _ToolCircuitBreakerTracker()
