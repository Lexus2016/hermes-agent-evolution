#!/usr/bin/env python3
"""Cron-cycle trajectory logger — records tool-call action sequences (#1215).

Records the sequence of tool calls (name, key args, result summary, timestamp,
pass/fail) during a cron session to a JSON sidecar at
``~/.hermes/evolution/trajectories/<date>.json``.

WHY THIS EXISTS — trajectory-level safety monitoring (parent #1203).
A cron agent runs unattended for long horizons; the sequence of actions it
takes is the primary audit trail for detecting suspicious patterns (e.g.
a compromised skill silently calling ``write_file`` on a system path after
a benign ``read_file``).  Per-tool logging already exists in agent.log, but
the *sequence* — which tool followed which, and with what args — is not
preserved in a structured, queryable form.  This module provides that.

DESIGN — minimal surface, zero core coupling.
``TrajectoryLogger`` is a standalone context manager.  The cron scheduler
constructs one per job run, passes it the session_id, and the agent loop
calls ``log_tool_call()`` after each tool dispatch.  The logger appends to
an in-memory list and flushes to a JSON sidecar on close.  No core files
are modified — the scheduler integration is a single call site (added
separately if desired); the logger works as a standalone library today.

Usage::

    with TrajectoryLogger(session_id="cron-abc123") as tlog:
        tlog.log_tool_call("terminal", {"command": "ls"}, "ok", True)
        tlog.log_tool_call("write_file", {"path": "/tmp/x"}, "ok", True)
    # → ~/.hermes/evolution/trajectories/2026-07-23.json updated
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Secret-like argument keys whose values must be redacted before logging.
_REDACT_KEYS = frozenset({
    "api_key",
    "token",
    "password",
    "secret",
    "authorization",
    "credential",
    "key",
    "passphrase",
    "private_key",
})

_REDACT_PATTERNS = [
    re.compile(r"(api[_-]?key|token|password|secret)\s*[:=]\s*\S+", re.I),
]

# Max length for arg values and result summaries — keeps the sidecar small.
_MAX_VALUE_LEN = 500
_MAX_RESULT_LEN = 1000


def _redact_value(val: Any) -> Any:
    """Recursively redact secret-like values in nested dicts/lists."""
    if isinstance(val, dict):
        return {
            k: ("***REDACTED***" if k.lower() in _REDACT_KEYS else _redact_value(v))
            for k, v in val.items()
        }
    if isinstance(val, list):
        return [_redact_value(v) for v in val]
    if isinstance(val, str):
        s = val
        for pat in _REDACT_PATTERNS:
            s = pat.sub(r"\1=***REDACTED***", s)
        return s[:_MAX_VALUE_LEN]
    return val


def _truncate(s: str, max_len: int = _MAX_RESULT_LEN) -> str:
    """Truncate a string to ``max_len`` with an ellipsis indicator."""
    if len(s) <= max_len:
        return s
    return s[:max_len] + "...[truncated]"


def _trajectories_dir() -> Path:
    """Return the trajectory sidecar directory under HERMES_HOME."""
    from hermes_constants import get_hermes_home

    d = get_hermes_home() / "evolution" / "trajectories"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _sidecar_path(date_str: Optional[str] = None) -> Path:
    """Return the JSON sidecar path for a given date (YYYY-MM-DD)."""
    if date_str is None:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return _trajectories_dir() / f"{date_str}.json"


class TrajectoryLogger:
    """Context-managed trajectory logger for a single cron session.

    Accumulates tool-call records in memory and flushes them to a JSON
    sidecar on close.  The sidecar is a list of session objects, each
    containing a ``tool_calls`` array.  Multiple sessions on the same day
    are appended to the same file.

    Attributes:
        session_id: Identifier for the cron session being logged.
        entries: In-memory list of tool-call records.
    """

    def __init__(
        self, session_id: str, job_name: str = "", date_str: Optional[str] = None
    ) -> None:
        self.session_id = session_id
        self.job_name = job_name
        self._date_str = date_str
        self.entries: List[Dict[str, Any]] = []
        self._start_ts = time.time()
        self._closed = False

    def log_tool_call(
        self,
        tool_name: str,
        args: Dict[str, Any],
        result_summary: str,
        success: bool,
        error: Optional[str] = None,
    ) -> None:
        """Record a single tool call in the trajectory.

        Args:
            tool_name: Name of the tool invoked (e.g. ``"terminal"``).
            args: Tool arguments dict — secret-like keys are redacted.
            result_summary: Short text summary of the tool's output.
            success: Whether the tool call succeeded.
            error: Error message if the call failed, else None.
        """
        if self._closed:
            return
        entry: Dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "tool": tool_name,
            "args": _redact_value(args),
            "result": _truncate(str(result_summary)),
            "success": success,
        }
        if error:
            entry["error"] = _truncate(str(error), _MAX_VALUE_LEN)
        self.entries.append(entry)

    def close(self) -> Path:
        """Flush the accumulated trajectory to the JSON sidecar.

        Returns the path to the written sidecar.  Idempotent — calling
        close() twice writes only once.
        """
        if self._closed:
            return _sidecar_path(self._date_str)
        self._closed = True

        session_record: Dict[str, Any] = {
            "session_id": self.session_id,
            "job_name": self.job_name,
            "start_ts": self._start_ts,
            "end_ts": time.time(),
            "tool_call_count": len(self.entries),
            "tool_calls": self.entries,
        }

        path = _sidecar_path(self._date_str)
        existing: List[Dict[str, Any]] = []
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(existing, list):
                    existing = []
            except (json.JSONDecodeError, OSError):
                existing = []

        existing.append(session_record)
        try:
            path.write_text(
                json.dumps(existing, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("Failed to write trajectory sidecar %s: %s", path, exc)
        return path

    def __enter__(self) -> "TrajectoryLogger":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()
