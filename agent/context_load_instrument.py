#!/usr/bin/env python3
"""Context-file load instrumentation (issue #2442, Slice A).

The AGENTS.md evaluation harness (parent #2252) needs to answer, for any
given session, *"which context files actually loaded, and what did the loader
do with them?"* — before it can assert that the right rules were applied.

This slice delivers the edges-only, high-land-confidence piece: a
process-local recorder that captures a structured ``LoadEvent`` for every
context file the prompt builder touches, with no change to the loading
*business logic* (which file wins, how it's scanned, how it's capped) beyond
a thin ``record_context_load(...)`` call at each loader's natural return
point.

Contract
--------
* **Zero-cost when disabled**: recording is a no-op unless enabled. The
  recorder never raises into the prompt-assembly path (the system prompt is
  sacred); any failure degrades to a dropped event.
* **Process-local only**: no file I/O, no telemetry. The harness reads events
  via ``drain_events()`` in-process.
* **Edges-only**: callers record *facts the loader already computed* (path,
  kind, char count, blocked flag) — nothing is recomputed or re-scanned.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

# Context-file kinds, mirroring prompt_builder's priority tiers.
KIND_HERMES = "hermes"
KIND_AGENTS = "agents"
KIND_CLAUDE = "claude"
KIND_CURSORRULES = "cursorrules"
KIND_SOUL = "soul"

_ALL_KINDS = {KIND_HERMES, KIND_AGENTS, KIND_CLAUDE, KIND_CURSORRULES, KIND_SOUL}


@dataclass
class LoadEvent:
    """One context-file load, as observed by the recorder."""

    path: str
    kind: str
    chars: int = 0
    loaded: bool = True  # False when the file was found-but-unreadable/empty
    blocked: bool = False  # True when threat-scan replaced content
    skipped: bool = False  # True when a higher-priority source won
    section: str = ""  # the rendered ``## label`` header, if any

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "kind": self.kind,
            "chars": self.chars,
            "loaded": self.loaded,
            "blocked": self.blocked,
            "skipped": self.skipped,
            "section": self.section,
        }


class ContextLoadRecorder:
    """Thread-safe, bounded buffer of ``LoadEvent``s for the eval harness."""

    _MAX_EVENTS = 4096

    def __init__(self) -> None:
        self._events: List[LoadEvent] = []
        self._lock = threading.Lock()

    def record(self, event: LoadEvent) -> None:
        """Append an event, bounded and never raising."""
        try:
            with self._lock:
                self._events.append(event)
                if len(self._events) > self._MAX_EVENTS:
                    del self._events[: len(self._events) - self._MAX_EVENTS]
        except Exception:  # pragma: no cover — never fail the loader
            return

    def drain(self) -> List[LoadEvent]:
        """Return a copy of all recorded events and clear the buffer."""
        with self._lock:
            events = list(self._events)
            self._events = []
        return events

    def snapshot(self) -> List[LoadEvent]:
        """Return a copy of all recorded events without clearing."""
        with self._lock:
            return list(self._events)


_recorder = ContextLoadRecorder()
_recording_enabled = False


def enable_recording() -> None:
    """Turn on context-load recording (idempotent)."""
    global _recording_enabled
    _recording_enabled = True


def disable_recording() -> None:
    """Turn off recording and clear the buffer."""
    global _recording_enabled
    _recording_enabled = False
    _recorder.drain()


def is_recording() -> bool:
    return _recording_enabled


def record_context_load(
    path: str,
    kind: str,
    *,
    chars: int = 0,
    loaded: bool = True,
    blocked: bool = False,
    skipped: bool = False,
    section: str = "",
) -> None:
    """Record one context-file load. No-op unless recording is enabled.

    Safe to call from the hot prompt-assembly path: when disabled this returns
    immediately; when enabled it never raises and drops events on overflow.
    """
    if not _recording_enabled:
        return
    if kind not in _ALL_KINDS:
        kind = "unknown"
    try:
        chars_int = int(chars)
    except (TypeError, ValueError):
        chars_int = 0
    _recorder.record(
        LoadEvent(
            path=str(path),
            kind=kind,
            chars=chars_int,
            loaded=bool(loaded),
            blocked=bool(blocked),
            skipped=bool(skipped),
            section=section,
        )
    )


def drain_events() -> List[LoadEvent]:
    """Drain and return recorded events (the eval harness's read path)."""
    return _recorder.drain()


def snapshot_events() -> List[LoadEvent]:
    """Peek at recorded events without clearing them."""
    return _recorder.snapshot()


def loaded_paths(kind: Optional[str] = None) -> List[str]:
    """Convenience: distinct paths of successfully-loaded context files.

    Useful for a one-line eval assertion ("the harness loaded AGENTS.md").
    """
    seen: List[str] = []
    for e in snapshot_events():
        if not e.loaded or e.skipped:
            continue
        if kind is not None and e.kind != kind:
            continue
        if e.path not in seen:
            seen.append(e.path)
    return seen


__all__ = [
    "LoadEvent",
    "ContextLoadRecorder",
    "record_context_load",
    "enable_recording",
    "disable_recording",
    "is_recording",
    "drain_events",
    "snapshot_events",
    "loaded_paths",
    "KIND_HERMES",
    "KIND_AGENTS",
    "KIND_CLAUDE",
    "KIND_CURSORRULES",
    "KIND_SOUL",
]
