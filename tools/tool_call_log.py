"""Tool-call log with semantic-intent / syntactic-noise field classification.

#2236 — Slice A of replay-or-fork semantics (#2225).

Non-atomic tools (those with irreversible real-world side effects — sending an
email, charging a payment, consuming a single-use token) are cataloged. Every
call to such a tool is recorded with two classes of arguments:

- **semantic-intent** fields — what the user actually wants to happen
  (``recipient``, ``amount``, ``memo``). Two calls with the same semantic
  intent are *the same action* and must not be re-executed after a restore.
- **syntactic-noise** fields — per-call randomness that should NOT be treated
  as intent (``trace_id``, ``nonce``, ``request_id``). The LLM re-synthesizes
  these on every call, so a bare argument hash would wrongly signal "different
  call" after restore and defeat server-side idempotency detection.

The analyzer classifies each field and infers a stable **idempotency key**
from the semantic-intent fields alone. Slice B (#2237) will consult this key
at checkpoint-restore time to replay-or-fork.

This module is standalone — it records and classifies only. Wiring into the
MCP dispatch path happens in Slice B.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── Non-atomic tool registry ────────────────────────────────────────────
#
# Tools with irreversible real-world side effects. A checkpoint-restore that
# re-runs one of these can cause a duplicate send, double charge, or token
# resurrection. Read-only / idempotent tools (search, read, list) are NOT
# listed — replaying them is harmless.

# Canonical tool names. The MCP prefix mirrors how tools are surfaced to the
# agent (``mcp__<server>__<tool>``); bare names cover non-MCP built-ins.
NON_ATOMIC_TOOLS: Dict[str, "NonAtomicToolSpec"] = {}


@dataclass(frozen=True)
class NonAtomicToolSpec:
    """Catalog entry for an irreversible tool.

    ``semantic_fields`` — argument keys that define the action's intent.
    ``noise_fields`` — argument keys that are per-call randomness.
    Unlisted fields default to noise (safer: only an explicit match counts
    as repeated intent).
    """

    name: str
    semantic_fields: tuple[str, ...] = ()
    noise_fields: tuple[str, ...] = ()


def register_non_atomic_tool(
    name: str,
    semantic_fields: tuple[str, ...] = (),
    noise_fields: tuple[str, ...] = (),
) -> None:
    """Register a tool as non-atomic (irreversible side effect)."""
    spec = NonAtomicToolSpec(
        name=name,
        semantic_fields=tuple(f.lower() for f in semantic_fields),
        noise_fields=tuple(f.lower() for f in noise_fields),
    )
    NON_ATOMIC_TOOLS[name.lower()] = spec


# Seed registry with the known irreversible tool families. The canonical
# names mirror the MCP server tool-naming convention used elsewhere.
register_non_atomic_tool(
    "agentmail__send_message",
    semantic_fields=("to", "subject", "body", "cc", "bcc"),
    noise_fields=("message_id", "request_id", "trace_id"),
)
register_non_atomic_tool(
    "agentmail__send_draft",
    semantic_fields=("draft_id", "to"),
    noise_fields=("request_id",),
)
register_non_atomic_tool(
    "github__create_issue",
    semantic_fields=("owner", "repo", "title", "body"),
    noise_fields=("client_mutation_id",),
)
register_non_atomic_tool(
    "github__create_repo",
    semantic_fields=("name", "owner"),
    noise_fields=("client_mutation_id",),
)
register_non_atomic_tool(
    "stripe__create_payment",
    semantic_fields=("amount", "currency", "customer", "description"),
    noise_fields=("idempotency_key", "request_id", "nonce"),
)

# Generic noise-field suffixes — any field matching these is noise regardless
# of the tool. Catches per-call randomness without an explicit registry entry.
_NOISE_FIELD_PATTERNS = (
    "trace_id",
    "request_id",
    "nonce",
    "idempotency_key",
    "client_mutation_id",
    "correlation_id",
    "x_request_id",
)


def is_non_atomic(tool_name: str) -> bool:
    """Return True if ``tool_name`` is registered as non-atomic.

    Matching is case-insensitive and also recognizes the MCP
    ``mcp__<server>__<tool>`` form by stripping the prefix.
    """
    key = tool_name.lower()
    if key in NON_ATOMIC_TOOLS:
        return True
    if "__" in key:
        # mcp__server__tool → try the server__tool tail
        parts = key.split("__")
        if len(parts) >= 2:
            tail = "__".join(parts[-2:])
            if tail in NON_ATOMIC_TOOLS:
                return True
            bare = parts[-1]
            if bare in NON_ATOMIC_TOOLS:
                return True
    return False


def _get_spec(tool_name: str) -> Optional[NonAtomicToolSpec]:
    key = tool_name.lower()
    if key in NON_ATOMIC_TOOLS:
        return NON_ATOMIC_TOOLS[key]
    if "__" in key:
        parts = key.split("__")
        if len(parts) >= 2:
            tail = "__".join(parts[-2:])
            if tail in NON_ATOMIC_TOOLS:
                return NON_ATOMIC_TOOLS[tail]
            bare = parts[-1]
            if bare in NON_ATOMIC_TOOLS:
                return NON_ATOMIC_TOOLS[bare]
    return None


# ── Field classifier ────────────────────────────────────────────────────


@dataclass(frozen=True)
class ClassifiedFields:
    """Result of splitting a tool call's arguments by intent class."""

    semantic: Dict[str, Any] = field(default_factory=dict)
    noise: Dict[str, Any] = field(default_factory=dict)


def classify_fields(tool_name: str, arguments: Dict[str, Any]) -> ClassifiedFields:
    """Split ``arguments`` into semantic-intent and syntactic-noise fields.

    A field is semantic if it is listed in the tool's ``semantic_fields``.
    A field is noise if it matches a noise pattern OR is listed in
    ``noise_fields``. Fields that match neither default to **noise** —
    this is the safe direction, because treating an unknown field as intent
    would suppress a legitimate re-execution.
    """
    spec = _get_spec(tool_name)
    arguments = arguments or {}
    semantic: Dict[str, Any] = {}
    noise: Dict[str, Any] = {}

    for raw_key, value in arguments.items():
        key = str(raw_key).lower()
        if spec and key in spec.semantic_fields:
            semantic[raw_key] = value
        elif spec and key in spec.noise_fields:
            noise[raw_key] = value
        elif any(pat in key for pat in _NOISE_FIELD_PATTERNS):
            noise[raw_key] = value
        else:
            # Unknown field → default to noise (safe direction).
            noise[raw_key] = value

    return ClassifiedFields(semantic=semantic, noise=noise)


# ── Idempotency key inference ───────────────────────────────────────────


def infer_idempotency_key(tool_name: str, arguments: Dict[str, Any]) -> str:
    """Return a stable idempotency key derived from semantic-intent fields.

    Two calls produce the SAME key iff their semantic intent is identical,
    regardless of syntactic-noise fields. Used by Slice B (#2237) to decide
    replay (same key) vs fork (different key) on checkpoint-restore.
    """
    classified = classify_fields(tool_name, arguments)
    # Sort keys for deterministic serialization.
    payload = json.dumps(classified.semantic, sort_keys=True, default=str)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"{tool_name.lower()}:{digest[:16]}"


# ── Tool-call log ────────────────────────────────────────────────────────


@dataclass
class LoggedToolCall:
    """A recorded non-atomic tool call."""

    tool_name: str
    arguments: Dict[str, Any]
    idempotency_key: str
    classified: ClassifiedFields
    recorded_at: str
    result_digest: Optional[str] = None  # set when the result is observed


class ToolCallLog:
    """Thread-safe append-only log of non-atomic tool calls.

    The log is keyed by idempotency key so a restore-time lookup can answer
    "has this exact intent already executed?" in O(1). Slice B consults this
    to replay-or-fork.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: Dict[str, LoggedToolCall] = {}

    def record(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        result: Any = None,
    ) -> LoggedToolCall:
        """Record a non-atomic tool call. No-op for atomic tools.

        Returns the :class:`LoggedToolCall`. If the same idempotency key
        already exists, the existing entry is preserved (first-writer-wins)
        and its ``result_digest`` is updated if a ``result`` is supplied.
        """
        if not is_non_atomic(tool_name):
            # Atomic tools are never logged — replay is harmless.
            raise ValueError(f"{tool_name!r} is not a non-atomic tool")

        classified = classify_fields(tool_name, arguments)
        key = infer_idempotency_key(tool_name, arguments)
        entry = LoggedToolCall(
            tool_name=tool_name,
            arguments=dict(arguments or {}),
            idempotency_key=key,
            classified=classified,
            recorded_at=datetime.now(timezone.utc).isoformat(),
            result_digest=_digest_result(result),
        )
        with self._lock:
            existing = self._entries.get(key)
            if existing is not None:
                if result is not None:
                    existing.result_digest = entry.result_digest
                return existing
            self._entries[key] = entry
        return entry

    def lookup(
        self, tool_name: str, arguments: Dict[str, Any]
    ) -> Optional[LoggedToolCall]:
        """Return the logged entry for this intent, or None if unseen."""
        key = infer_idempotency_key(tool_name, arguments)
        with self._lock:
            return self._entries.get(key)

    def has_executed(self, tool_name: str, arguments: Dict[str, Any]) -> bool:
        """True if a call with identical semantic intent is logged."""
        return self.lookup(tool_name, arguments) is not None

    def all_entries(self) -> List[LoggedToolCall]:
        with self._lock:
            return list(self._entries.values())

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


def _digest_result(result: Any) -> Optional[str]:
    if result is None:
        return None
    try:
        payload = json.dumps(result, sort_keys=True, default=str)
    except (TypeError, ValueError):
        payload = str(result)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


# Module-level singleton — the default log used by the agent session.
# Slice B will reset/restore this on checkpoint-restore.
_default_log: Optional[ToolCallLog] = None
_default_log_lock = threading.Lock()


def get_default_log() -> ToolCallLog:
    """Return the process-wide default :class:`ToolCallLog`."""
    global _default_log
    with _default_log_lock:
        if _default_log is None:
            _default_log = ToolCallLog()
        return _default_log


def reset_default_log() -> ToolCallLog:
    """Clear and return the default log (used on checkpoint-restore)."""
    log = get_default_log()
    log.clear()
    return log
