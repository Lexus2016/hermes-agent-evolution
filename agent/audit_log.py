"""Structured audit event log (EU AI Act Art. 12) — #1718.

JSONL at ``$HERMES_HOME/logs/audit-events.jsonl`` recording tool calls as
immutable records exportable for regulatory review.  Gated via
``security.audit_log`` (default off), fail-open, profile-aware.
"""

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Optional

_log = logging.getLogger(__name__)
_lock = threading.Lock()
EVENT_TOOL_CALL_COMPLETE = "tool_call_complete"
EVENT_TOOL_CALL_BLOCKED = "tool_call_blocked"
_REDACTED = (
    "access_token",
    "refresh_token",
    "code",
    "code_verifier",
    "state",
    "ticket",
    "cookie",
    "Authorization",
    "authorization",
    "api_key",
    "token",
    "password",
    "secret",
    "GITHUB_TOKEN",
)


def _home() -> Path:
    env = os.environ.get("HERMES_HOME")
    return Path(env) if env else Path.home() / ".hermes"


def _log_path() -> Path:
    return _home() / "logs" / "audit-events.jsonl"


def _is_enabled() -> bool:
    """``security.audit_log`` via ``load_config_readonly()`` (not raw yaml)."""
    try:
        from hermes_cli.config import load_config_readonly

        return bool(
            (load_config_readonly() or {}).get("security", {}).get("audit_log", False)
        )
    except Exception:
        return False


def log_audit_event(
    event: str,
    *,
    session_id: str = "",
    turn_id: str = "",
    tool_call_id: str = "",
    tool_name: str = "",
    task_id: str = "",
    **fields: Any,
) -> None:
    """Append one immutable JSONL audit event.  Gated + fail-open."""
    if not _is_enabled():
        return
    safe = {k: v for k, v in fields.items() if k not in _REDACTED}
    import datetime as _dt

    entry = {
        "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "event": event,
        "session_id": session_id,
        "turn_id": turn_id,
        "tool_call_id": tool_call_id,
        "tool_name": tool_name,
        "task_id": task_id,
        **safe,
    }
    line = json.dumps(entry, separators=(",", ":"), default=str) + "\n"
    try:
        path = _log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with _lock:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
    except Exception as e:
        _log.warning("audit log write failed: %s", e)


def export_audit_jsonl(output_path: Optional[str] = None) -> str:
    """Copy the log to *output_path* (returning it) or return its contents."""
    src = _log_path()
    if output_path:
        import shutil

        shutil.copy2(str(src), output_path)
        return output_path
    return src.read_text(encoding="utf-8") if src.exists() else ""
