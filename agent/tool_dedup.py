"""Tool-call deduplication tracker (When2Tool, issue #2282).

Reduces unnecessary tool calls by tracking recent *successful* tool
invocations per session and surfacing a hint when the model re-issues the
same tool+arguments combination that already succeeded.

This is the cache-safe, prompt-free slice of the When2Tool proposal. It
deliberately does NOT inject a self-check prompt mid-conversation (that
would break prompt caching and message-role alternation) and does NOT
require hidden-state access (that only applies to local open-weight
models). Instead it records what actually ran and succeeded, and lets the
model decide whether to reuse the prior result.

Design invariants:

* **Side-effect-free on the conversation.** The tracker only *returns* a
  hint string that the caller may append to a tool result. It never
  mutates the system prompt, never injects a synthetic user message, and
  never alters message-role alternation.
* **Fail-open.** Any error resolving config, normalizing args, or reading
  state results in *no hint* — the tracker can never block a legitimate
  call. It is advisory, not a gate.
* **Session-scoped.** State is keyed by ``session_id`` so one session's
  calls never leak into another (subagents, kanban workers, and gateway
  sessions stay isolated).
* **Success-only.** Only *successful* prior calls are recorded, so a
  previously-failed call is never treated as "already done".
* **Bounded.** Per-session history is capped (LRU-ish) so memory does not
  grow without bound over a long session.

The module depends only on the Python standard library, is import-safe,
and is unit-testable in isolation.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from collections import OrderedDict
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

#: Default config. Overridable via ``tool_dedup.enabled`` in config.yaml or
#: the ``HERMES_TOOL_DEDUP`` env var.
DEFAULT_CONFIG: Dict[str, Any] = {
    # Master switch. Default ON (advisory + fail-open, so it is low-risk).
    "enabled": True,
    # How many distinct tool+args entries to remember per session.
    "max_entries_per_session": 64,
    # Only flag a repeat if the prior successful call happened within this
    # many *distinct* tool calls ago (a loose recency window). 0 disables
    # the recency check (flag any prior success).
    "recency_window": 20,
}

_TOOL_DEDUP_ENV = "HERMES_TOOL_DEDUP"

#: Tool categories the tracker applies to. These are the read/search/web
#: tools the When2Tool finding targets — the ones most often called
#: redundantly when the answer is already in context.
_TRACKED_TOOLS = {
    "read_file",
    "search_files",
    "web_search",
    "web_extract",
    "tavily_search",
    "browser_navigate",
}

#: Argument keys that are volatile per-call and must be stripped before
#: computing the dedup key (they would otherwise make every call look
#: unique). ``task_id``/``session_id`` are transport plumbing, not part of
#: the tool's semantic identity.
_VOLATILE_ARG_KEYS = {"task_id", "session_id", "tool_call_id", "turn_id"}


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


class _SessionTracker:
    """Per-session record of recent successful tool calls.

    Thread-safe. ``_history`` is an ``OrderedDict`` keyed by a stable hash
    of (tool, normalized-args); the value is the number of distinct tool
    calls since that entry was last seen (used for the recency window).
    """

    __slots__ = ("_history", "_lock", "_call_count")

    def __init__(self) -> None:
        self._history: "OrderedDict[str, int]" = OrderedDict()
        self._lock = threading.Lock()
        self._call_count = 0

    def record(self, key: str, max_entries: int) -> None:
        """Record a successful call, resetting its recency counter."""
        with self._lock:
            self._call_count += 1
            self._history[key] = 0
            self._history.move_to_end(key)
            # Bound memory: drop the oldest entries beyond the cap.
            while len(self._history) > max_entries:
                self._history.popitem(last=False)

    def recent(self, key: str, window: int) -> Optional[int]:
        """Return the recency (distinct calls since) of *key*, or None.

        ``window`` <= 0 means "any prior success counts". Returns the
        recency distance if the key is present and within the window.
        """
        with self._lock:
            if key not in self._history:
                return None
            dist = self._history[key]
            if window > 0 and dist > window:
                return None
            return dist

    def tick(self) -> None:
        """Advance the recency counter for all tracked entries.

        Called once per tracked tool call (successful or not) so the
        recency window measures *distinct tool calls*, not wall-clock time.
        """
        with self._lock:
            self._call_count += 1
            for k in list(self._history.keys()):
                self._history[k] += 1

    def clear(self) -> None:
        with self._lock:
            self._history.clear()
            self._call_count = 0


#: Process-global registry of per-session trackers. Keyed by session_id
#: (``"default"`` when none is provided). Guarded by ``_REGISTRY_LOCK``.
_REGISTRY: Dict[str, _SessionTracker] = {}
_REGISTRY_LOCK = threading.Lock()


def _tracker_for(session_id: Optional[str]) -> _SessionTracker:
    sid = session_id or "default"
    with _REGISTRY_LOCK:
        tr = _REGISTRY.get(sid)
        if tr is None:
            tr = _SessionTracker()
            _REGISTRY[sid] = tr
        return tr


# ---------------------------------------------------------------------------
# Config gate
# ---------------------------------------------------------------------------


def tool_dedup_enabled() -> bool:
    """Whether the tool-call dedup tracker is active.

    Default **ON** (advisory + fail-open, so it is low-risk). Escape
    hatches (in priority order):

    * ``HERMES_TOOL_DEDUP`` env var — ``0``/``false``/``no``/``off``
      disables; any truthy value forces ON.
    * ``tool_dedup.enabled`` in ``config.yaml`` — set ``false`` to disable
      persistently.

    Any failure resolving config -> ON (the safe default for an advisory,
    fail-open tracker).
    """
    env = os.environ.get(_TOOL_DEDUP_ENV)
    if env is not None:
        return env.strip().lower() in {"1", "true", "yes", "on"}
    try:
        from hermes_cli.config import load_config as _load_config

        cfg = _load_config() or {}
    except Exception:
        return True
    section = cfg.get("tool_dedup") if isinstance(cfg, dict) else None
    if isinstance(section, dict) and "enabled" in section:
        return bool(section.get("enabled"))
    return True


def _config_value(key: str, default: Any) -> Any:
    try:
        from hermes_cli.config import load_config as _load_config

        cfg = _load_config() or {}
    except Exception:
        return default
    section = cfg.get("tool_dedup") if isinstance(cfg, dict) else None
    if isinstance(section, dict) and key in section:
        return section.get(key)
    return default


# ---------------------------------------------------------------------------
# Key computation
# ---------------------------------------------------------------------------


def _normalize_args(args: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Return a stable, sortable copy of *args* for dedup keying.

    Strips volatile transport keys and normalizes values (recursively
    sorts dict keys, converts non-JSON types to strings) so that
    semantically-identical calls hash the same regardless of argument
    ordering or incidental type differences.
    """
    if not isinstance(args, dict):
        return {}
    cleaned: Dict[str, Any] = {}
    for k, v in args.items():
        if k in _VOLATILE_ARG_KEYS:
            continue
        cleaned[k] = _normalize_value(v)
    return cleaned


def _normalize_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _normalize_value(v) for k, v in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_normalize_value(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    # Fall back to a stable string for anything else (paths, enums, etc.).
    try:
        return str(value)
    except Exception:
        return "<unserializable>"


def _dedup_key(function_name: str, args: Optional[Dict[str, Any]]) -> str:
    """Stable hash of (tool, normalized-args) for dedup tracking."""
    payload = json.dumps(
        {"tool": function_name, "args": _normalize_args(args)},
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def check_tool_dedup(
    function_name: str,
    function_args: Optional[Dict[str, Any]] = None,
    session_id: Optional[str] = None,
) -> Optional[str]:
    """Return a hint string if *function_name*+args was recently successful.

    Returns ``None`` when there is no recent successful duplicate (or when
    the tracker is disabled / the tool is not tracked / an error occurs).
    The returned string is advisory — the caller may append it to the tool
    result so the model can decide whether to reuse the prior result.

    Calls with no meaningful arguments (empty or all-volatile) are never
    flagged: an empty-args call carries no reusable result, so there is
    nothing to reuse.
    """
    if function_name not in _TRACKED_TOOLS:
        return None
    if not tool_dedup_enabled():
        return None
    try:
        normalized = _normalize_args(function_args)
        if not normalized:
            return None
        window = int(_config_value("recency_window", DEFAULT_CONFIG["recency_window"]))
        key = _dedup_key(function_name, function_args)
        tr = _tracker_for(session_id)
        dist = tr.recent(key, window)
        if dist is None:
            return None
        return (
            f"[dedup] You already called {function_name} with these arguments "
            f"{dist} tool-call(s) ago and it succeeded. Reuse that result "
            "instead of calling it again unless the underlying data may have "
            "changed."
        )
    except Exception as exc:  # fail-open
        logger.debug("tool_dedup check error: %s", exc)
        return None


def record_tool_call(
    function_name: str,
    function_args: Optional[Dict[str, Any]] = None,
    session_id: Optional[str] = None,
    success: bool = True,
) -> None:
    """Record a tool call for dedup tracking.

    *success=True* records the (tool, args) key so a later identical call
    is flagged. *success=False* only advances the recency counter (a failed
    call must not be treated as "already done"). Non-tracked tools only
    advance the counter so the recency window measures distinct calls.
    """
    if not tool_dedup_enabled():
        return
    try:
        tr = _tracker_for(session_id)
        if function_name in _TRACKED_TOOLS:
            if success:
                max_entries = int(
                    _config_value(
                        "max_entries_per_session",
                        DEFAULT_CONFIG["max_entries_per_session"],
                    )
                )
                tr.record(_dedup_key(function_name, function_args), max_entries)
            else:
                tr.tick()
        else:
            tr.tick()
    except Exception as exc:  # fail-open
        logger.debug("tool_dedup record error: %s", exc)


def reset_tool_dedup(session_id: Optional[str] = None) -> None:
    """Clear dedup state for a session (or all sessions when *None*).

    Called after context compression — the prior result has been summarised
    away, so the model legitimately needs to re-read/re-search.
    """
    try:
        if session_id is None:
            with _REGISTRY_LOCK:
                for tr in _REGISTRY.values():
                    tr.clear()
        else:
            _tracker_for(session_id).clear()
    except Exception as exc:  # fail-open
        logger.debug("tool_dedup reset error: %s", exc)


__all__ = [
    "DEFAULT_CONFIG",
    "check_tool_dedup",
    "record_tool_call",
    "reset_tool_dedup",
    "tool_dedup_enabled",
]
