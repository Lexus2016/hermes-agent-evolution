"""Refusal nudge telemetry sidecar (#1265).

Records per-nudge and per-transition events so future cycles can measure which
nudge tiers shift model behavior vs which are ignored. Mirrors the
``tools/skill_usage.py`` sidecar pattern: atomic writes, cross-process file
lock (fcntl/msvcrt), best-effort (never breaks the conversation loop).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

# fcntl is Unix-only; on Windows use msvcrt for file locking.
msvcrt = None
try:
    import fcntl
except ImportError:  # pragma: no cover - platform-specific fallback
    fcntl = None
    try:
        import msvcrt
    except ImportError:
        pass

# Cap the number of events stored to prevent unbounded growth.  When the
# cap is reached the oldest events are dropped first (FIFO eviction).
_MAX_EVENTS = 5000


def _is_disabled() -> bool:
    """Return True when telemetry should be skipped (e.g. import failed)."""
    return False


def _telemetry_file() -> Path:
    """Return the path to the refusal telemetry sidecar."""
    return get_hermes_home() / ".refusal_telemetry.json"


@contextmanager
def _telemetry_lock():
    """Serialize sidecar read-modify-write cycles across processes."""
    lock_path = _telemetry_file().with_suffix(".json.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    if fcntl is None and msvcrt is None:
        yield
        return

    if msvcrt and (not lock_path.exists() or lock_path.stat().st_size == 0):
        lock_path.write_text(" ", encoding="utf-8")

    fd = open(lock_path, "r+" if msvcrt else "a+", encoding="utf-8")
    try:
        if fcntl:
            fcntl.flock(fd, fcntl.LOCK_EX)
        else:
            fd.seek(0)
            msvcrt.locking(fd.fileno(), msvcrt.LK_LOCK, 1)
        yield
    finally:
        if fcntl:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except (OSError, IOError):
                pass
        elif msvcrt:
            try:
                fd.seek(0)
                msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)
            except (OSError, IOError):
                pass
        fd.close()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_events() -> List[Dict[str, Any]]:
    """Load existing events from the sidecar. Returns an empty list on any error."""
    path = _telemetry_file()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("events"), list):
            return data["events"]
        if isinstance(data, list):  # legacy flat-list format
            return data
    except (json.JSONDecodeError, OSError, ValueError) as e:
        logger.debug("refusal_telemetry: could not read sidecar: %s", e)
    return []


def _save_events(events: List[Dict[str, Any]]) -> None:
    """Atomically write events to the sidecar."""
    path = _telemetry_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "events": events[-_MAX_EVENTS:],  # keep the most recent
    }
    try:
        fd, tmp = tempfile.mkstemp(
            dir=str(path.parent), prefix=".refusal_telemetry_", suffix=".tmp"
        )
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, path)
    except (OSError, IOError) as e:
        logger.debug("refusal_telemetry: could not write sidecar: %s", e)


def record_nudge(
    *,
    session_id: Optional[str],
    nudge_tier: str,
    refusal_category: str,
    nudge_count: int,
    session_refusal_count: int,
) -> None:
    """Record a nudge event (advisory / directive / session tier)."""
    event = {
        "type": "nudge",
        "timestamp": _now_iso(),
        "session_id": session_id or "",
        "nudge_tier": nudge_tier,
        "refusal_category": refusal_category,
        "nudge_count": nudge_count,
        "session_refusal_count": session_refusal_count,
    }
    try:
        with _telemetry_lock():
            events = _load_events()
            events.append(event)
            _save_events(events)
    except Exception as e:  # never break the conversation loop
        logger.debug("refusal_telemetry: record_nudge failed: %s", e)


def record_transition(
    *,
    session_id: Optional[str],
    nudge_tier: str,
    category_before: str,
    category_after: str,
    recovered: bool,
    took_action: bool,
) -> None:
    """Record a transition — the model's response after a nudge."""
    event = {
        "type": "transition",
        "timestamp": _now_iso(),
        "session_id": session_id or "",
        "nudge_tier": nudge_tier,
        "category_before": category_before,
        "category_after": category_after,
        "recovered": recovered,
        "took_action": took_action,
    }
    try:
        with _telemetry_lock():
            events = _load_events()
            events.append(event)
            _save_events(events)
    except Exception as e:  # never break the conversation loop
        logger.debug("refusal_telemetry: record_transition failed: %s", e)


def record_nudge_and_set_pending(
    agent, refusal_category: str, nudge_tier: str, nudge_count: int
) -> None:
    """Record a nudge event and stash pending telemetry on the agent (#1265).

    Convenience helper for the conversation loop: records the nudge, then
    stores ``{tier, category}`` on ``agent._pending_nudge_telemetry`` so
    the next response can record a transition.
    """
    if _is_disabled():
        return
    try:
        record_nudge(
            session_id=getattr(agent, "session_id", None),
            nudge_tier=nudge_tier,
            refusal_category=refusal_category,
            nudge_count=nudge_count,
            session_refusal_count=getattr(agent, "_session_refusal_count", 0),
        )
    except Exception:
        pass
    agent._pending_nudge_telemetry = {"tier": nudge_tier, "category": refusal_category}


def record_transition_if_pending(agent, category_after: str, took_action: bool) -> None:
    """Record a transition if a nudge is pending on the agent (#1265).

    Called when the model produces a new response after a nudge.  If a
    pending nudge exists, records the transition and clears the pending
    state.  ``took_action=True`` when the model produced tool calls.
    """
    if _is_disabled():
        return
    pt = getattr(agent, "_pending_nudge_telemetry", None)
    if not pt:
        return
    try:
        record_transition(
            session_id=getattr(agent, "session_id", None),
            nudge_tier=pt.get("tier", ""),
            category_before=pt.get("category", ""),
            category_after=category_after,
            recovered=not category_after,
            took_action=took_action,
        )
    except Exception:
        pass
    agent._pending_nudge_telemetry = None


def load_events() -> List[Dict[str, Any]]:
    """Public read accessor — returns all events from the sidecar."""
    try:
        with _telemetry_lock():
            return _load_events()
    except Exception as e:
        logger.debug("refusal_telemetry: load_events failed: %s", e)
        return []