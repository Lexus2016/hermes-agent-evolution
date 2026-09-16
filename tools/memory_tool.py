#!/usr/bin/env python3
"""Memory Tool - persistent curated memory (MEMORY.md = agent notes, USER.md = user
profile). Both enter the system prompt as a FROZEN snapshot at session start;
mid-session writes hit disk but never change the prompt (prefix cache intact).
Single `memory` tool: add/replace/remove or a batch `operations` list."""

import copy
import json
import logging
from contextvars import ContextVar
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

from hermes_constants import get_hermes_home
from utils import is_truthy_value
from tools.registry import no_cache_check_fn, registry, tool_error

# fcntl is Unix-only; on Windows use msvcrt for file locking
msvcrt = None
try:
    import fcntl
except ImportError:
    fcntl = None
    try:
        import msvcrt
    except ImportError:
        pass

logger = logging.getLogger(__name__)

_memory_surface_flags: ContextVar[Optional[Tuple[bool, bool]]] = ContextVar(
    "memory_surface_flags", default=None
)

def get_memory_dir() -> Path:
    """Return the profile-scoped memories directory."""
    return get_hermes_home() / "memories"

from tools.memory_tool_store import (
    ENTRY_DELIMITER,
    MEMORY_BLOCK_HEADERS,
    BLOCKED_WARNINGS_FILE,
    BLOCKED_WARNINGS_MAX,
    SOURCE_CLASSES,
    TRUST_TIERS,
    DEFAULT_SOURCE_CLASS,
    DEFAULT_TRUST_TIER,
    _PROV_OPEN,
    _PROV_CLOSE,
    _trust_rank,
    encode_provenance,
    parse_provenance,
    _scan_memory_content,
    _make_provenance,
    _log_guard_event,
    _validate_provenance,
    _drift_error,
    _read_failed_error,
    DEFAULT_MEMORY_CHAR_LIMIT,
    DEFAULT_USER_CHAR_LIMIT,
    MemoryStore,
)

def load_on_disk_store() -> "MemoryStore":
    """Fresh on-disk MemoryStore with configured limits/flags for contexts with no live
    agent (gateway, Desktop, ``/memory``) so approvals enforce the SAME caps as
    ``agent_init``. Falls back to defaults if config can't load; never raises."""
    memory_char_limit = DEFAULT_MEMORY_CHAR_LIMIT
    user_char_limit = DEFAULT_USER_CHAR_LIMIT
    memory_enabled = True
    user_profile_enabled = True
    allow_batch_override = False
    try:
        from hermes_cli.config import load_config
        config = load_config() or {}
        mem_cfg = get_builtin_memory_config(config)
        memory_enabled, user_profile_enabled = get_builtin_memory_store_flags(config)
        memory_char_limit = int(mem_cfg.get("memory_char_limit", memory_char_limit))
        user_char_limit = int(mem_cfg.get("user_char_limit", user_char_limit))
        allow_batch_override = bool(mem_cfg.get("allow_batch_memory_char_limit_override", False))
    except Exception:
        pass
    store = MemoryStore(
        memory_char_limit=memory_char_limit,
        user_char_limit=user_char_limit,
        allow_batch_override=allow_batch_override,
        memory_enabled=memory_enabled,
        user_profile_enabled=user_profile_enabled,
    )
    store.load_from_disk()
    return store

_BG_DELETE_ACTIONS = ("replace", "remove")

def _batch_op_line(op: Dict[str, Any]) -> str:
    op = op or {}
    act, content, old = op.get("action", "?"), op.get("content") or op.get("new_text") or "", op.get("old_text", "")
    if act == "remove":
        return f"- remove: {old}"
    return f"- replace: {old} -> {content}" if act == "replace" else f"- {act}: {content}"

def _background_delete_gate(action, operations, target="memory", content=None, old_text=None) -> Optional[str]:
    """Fail-closed operation gate for unattended background-review forks (#105921)."""
    from tools.skill_provenance import is_unattended_review

    if not is_unattended_review():
        return None
    hit = action in _BG_DELETE_ACTIONS or any(
        isinstance(op, dict) and op.get("action") in _BG_DELETE_ACTIONS for op in (operations or []))
    if not hit:
        return None
    payload = ({"action": "batch", "target": target, "operations": operations}
               if operations is not None else
               {"action": action, "target": target, "content": content, "old_text": old_text})
    detail = ("; ".join(_batch_op_line(op) for op in operations) if operations is not None
              else _batch_op_line({"action": action, "content": content, "old_text": old_text}))
    try:
        from tools import write_approval as wa
        record = wa.stage_write(
            wa.MEMORY, payload,
            summary=(f"background review consolidation ({'batch' if operations is not None else action} "
                     f"on {target}): {detail}")[:200],
            origin=wa.current_origin())
        return json.dumps({
            "success": True, "staged": True, "proposal_staged": True, "pending_id": record["id"],
            "message": ("Background review may not delete memory entries unattended. The proposed "
                        f"{'batch' if operations is not None else action} was staged for your approval — "
                        "review it with /memory pending (approve to apply, discard to drop)."),
        }, ensure_ascii=False)
    except Exception:
        logger.warning("Failed to stage background-review consolidation; denying", exc_info=True)
        return tool_error(
            "Background review may not delete memory entries ('replace'/'remove', including in a "
            "batch); 'add' is still available.", success=False)

def _apply_write_gate(
    action: str,
    target: str,
    content: Optional[str],
    old_text: Optional[str],
    source_class: str = DEFAULT_SOURCE_CLASS,
    trust_tier: str = DEFAULT_TRUST_TIER,
) -> Optional[str]:
    """Evaluate the memory write gate. Returns a JSON tool-result string when
    the write should NOT proceed normally (blocked or staged), or None when the
    caller should perform the real write.

    Only the mutating actions (add/replace/remove) are gated. Provenance tags
    (#316) ride along in the staged payload so an approved write keeps them.
    """
    if action not in {"add", "replace", "remove"}:
        return None

    try:
        from tools import write_approval as wa
    except Exception:
        # If the gate module can't load, fail open (current behaviour) rather
        # than blocking all memory writes.
        return None

    # Build a small inline summary/detail for the foreground approval prompt.
    label = "user profile" if target == "user" else "memory"
    if action == "add":
        summary = f"add to {label}"
        detail = content or ""
    elif action == "replace":
        summary = f"replace in {label}"
        detail = f"old: {old_text}\nnew: {content}"
    else:  # remove
        summary = f"remove from {label}"
        detail = old_text or ""

    decision = wa.evaluate_gate(wa.MEMORY, inline_summary=summary, inline_detail=detail)

    if decision.allow:
        return None

    if decision.blocked:
        return tool_error(decision.message, success=False)

    # stage
    payload = {
        "action": action,
        "target": target,
        "content": content,
        "old_text": old_text,
        "source_class": source_class,
        "trust_tier": trust_tier,
    }
    record = wa.stage_write(
        wa.MEMORY,
        payload,
        summary=f"{summary}: {detail[:120]}",
        origin=wa.current_origin(),
    )
    return json.dumps(
        {
            "success": True,
            "staged": True,
            "pending_id": record["id"],
            "message": decision.message,
        },
        ensure_ascii=False,
    )


def _apply_batch_write_gate(
    target: str, operations: List[Dict[str, Any]]
) -> Optional[str]:
    """Evaluate the write gate for a batch of memory operations.

    Returns a JSON tool-result string when the batch should NOT proceed
    (blocked or staged), or None when the caller should perform the real
    batch write. The whole batch is gated as a single unit.
    """
    try:
        from tools import write_approval as wa
    except Exception:
        return None

    label = "user profile" if target == "user" else "memory"
    summary = f"apply {len(operations)} op(s) to {label}"
    detail_lines = []
    for op in operations:
        op = op or {}
        act = op.get("action", "?")
        _op_content = op.get("content") or op.get("new_text") or ""
        if act == "remove":
            detail_lines.append(f"- remove: {op.get('old_text', '')}")
        elif act == "replace":
            detail_lines.append(
                f"- replace: {op.get('old_text', '')} -> {_op_content}"
            )
        else:
            detail_lines.append(f"- {act}: {_op_content}")
    detail = "\n".join(detail_lines)

    decision = wa.evaluate_gate(wa.MEMORY, inline_summary=summary, inline_detail=detail)

    if decision.allow:
        return None

    if decision.blocked:
        return tool_error(decision.message, success=False)

    payload = {"action": "batch", "target": target, "operations": operations}
    record = wa.stage_write(
        wa.MEMORY,
        payload,
        summary=f"{summary}: {detail[:120]}",
        origin=wa.current_origin(),
    )
    return json.dumps(
        {
            "success": True,
            "staged": True,
            "pending_id": record["id"],
            "message": decision.message,
        },
        ensure_ascii=False,
    )


def _missing_old_text_error(store: "MemoryStore", target: str, action: str) -> str:
    """Build a recoverable error for a replace/remove call that arrived without
    ``old_text``.

    ``replace``/``remove`` are inherently targeted -- without ``old_text`` there
    is no entry to act on, so we cannot fulfil the call. But returning a bare
    "old_text is required" is a dead-end: some structured-output clients omit the
    optional ``old_text`` field (it isn't, and can't be, schema-required without
    a top-level combinator the Codex backend rejects -- see
    tests/tools/test_memory_tool_schema.py). So instead we return the current
    entry inventory plus an explicit retry instruction, letting the model reissue
    the call with ``old_text`` set to a unique substring of the entry it means.
    Mirrors the batch path's ``_batch_error`` shape. (issues #43412, #49466)
    """
    entries = store._entries_for(target)
    current = store._char_count(target)
    limit = store._char_limit(target)
    return json.dumps(
        {
            "success": False,
            "error": (
                f"'{action}' needs old_text -- a short unique substring of the entry "
                f"to {action}. None was provided. Reissue the {action} with old_text "
                f"set to part of one of the current_entries below."
            ),
            "current_entries": entries,
            "usage": f"{current:,}/{limit:,}",
        },
        ensure_ascii=False,
    )


def _memory_enriched_error(exc: Exception, action: str) -> str:
    """Decompose an unclassified memory failure into a category with recovery hint.

    Issue #1648: 98% of memory failures are classified as opaque 'other'.
    This maps the underlying exception type to a concrete category + recovery
    suggestion so the agent can act instead of blind-retrying.
    """
    exc_type = type(exc).__name__
    exc_msg = str(exc)[:300]

    # Classify by exception family
    if isinstance(exc, (TimeoutError,)):
        category = "timeout"
        hint = (
            "The memory file may be locked by another process. Wait a moment and retry."
        )
    elif isinstance(exc, (PermissionError,)):
        category = "permission-denied"
        hint = "Check file permissions on the memory file in ~/.hermes/memory/."
    elif isinstance(exc, (UnicodeDecodeError,)):
        # Must precede ValueError — UnicodeDecodeError subclasses it (#2349).
        category = "encoding-corruption"
        hint = (
            "The memory file contains non-UTF-8 bytes. Use a terminal command "
            "to inspect the memory file, fix the encoding, then retry. The "
            "file was not modified."
        )
    elif isinstance(exc, (json.JSONDecodeError, ValueError)):
        category = "schema-mismatch"
        hint = "The memory file is corrupted. Consider using action='search' to verify state, then retry."
    elif isinstance(exc, (OSError, IOError)):
        category = "io-error"
        hint = "Disk I/O failed. Retry once; if it persists, proceed without memory persistence."
    elif isinstance(exc, (TypeError,)):
        category = "serialization-error"
        hint = "The content could not be serialized. Simplify the content and retry."
    elif isinstance(exc, (RuntimeError,)):
        # File-lock contention, atomic_write failures, internal state errors.
        # RuntimeError is the dominant "unexpected" type because the lock
        # retry path and atomic_write_text wrapper raise it (#2332/#2349).
        msg_lower = str(exc).lower()
        if "lock" in msg_lower or "timeout" in msg_lower:
            category = "lock-contention"
            hint = (
                "The memory file is locked by a concurrent write. Wait 1-2 "
                "seconds and retry the same operation. If it persists, "
                "proceed without memory — the fact can be saved in a later turn."
            )
        else:
            category = "internal-error"
            hint = (
                f"Memory store internal error ({exc_type}). Retry once; if it "
                f"fails identically, proceed with your reply and save memory "
                f"later. Do NOT loop on the same memory call."
            )
    elif isinstance(exc, (AttributeError, IndexError, KeyError)):
        category = "entry-state-mismatch"
        hint = (
            "The in-memory entry list is out of sync with disk (a concurrent "
            "session may have written between your read and write). Call "
            "action='search' to refresh the current state, then retry."
        )
    else:
        category = "unexpected"
        hint = (
            f"Unexpected error ({exc_type}). Retry the memory operation once; "
            f"if it fails identically, proceed with your reply and save the "
            f"fact in a later turn. Do NOT repeat the same call more than once."
        )

    logger.warning("memory_tool %s failed: %s: %s", action, exc_type, exc_msg)

    return json.dumps(
        {
            "success": False,
            "error": exc_msg,
            "error_type": exc_type,
            "error_category": category,
            "recovery_hint": hint,
            "action": action,
        },
        ensure_ascii=False,
    )


def memory_tool(
    action: str = None,
    target: str = "memory",
    content: str = None,
    old_text: str = None,
    new_text: str = None,
    source_class: str = DEFAULT_SOURCE_CLASS,
    trust_tier: str = DEFAULT_TRUST_TIER,
    source_filter: Optional[object] = None,
    min_trust: Optional[str] = None,
    include_superseded: bool = False,
    operations: Optional[List[Dict[str, Any]]] = None,
    target_size: Optional[int] = None,
    prefer: str = "longest",
    memory_char_limit: Optional[int] = None,
    store: Optional[MemoryStore] = None,
) -> str:
    """
    Single entry point for the memory tool. Dispatches to MemoryStore methods.

    Two shapes:
      - Single op: action + (content / old_text).
      - Batch:     operations=[{action, content?, old_text?}, ...] applied
                   atomically against the final char budget in ONE call.
    ``source_class`` / ``trust_tier`` tag provenance on add/replace (#316).
    ``source_filter`` / ``min_trust`` filter the ``search`` action's results.
    ``memory_char_limit`` is an optional per-batch override for target='memory'
    that is only honoured when ``store.allow_batch_override`` is True (issue #517).

    ``new_text`` is accepted as an alias for ``content`` on both shapes. The
    replace/remove ops target by ``old_text`` and supply the replacement via
    ``content``; callers naturally reach for ``new_text`` to mirror
    ``old_text`` (it's the patch tool's ``old_string``/``new_string`` shape),
    which silently left ``content`` empty and errored. Coalescing here removes
    that trap.

    ``new_text`` is accepted as an alias for ``content`` on both shapes. The
    replace/remove ops target by ``old_text`` and supply the replacement via
    ``content``; callers naturally reach for ``new_text`` to mirror
    ``old_text`` (it's the patch tool's ``old_string``/``new_string`` shape),
    which silently left ``content`` empty and errored. Coalescing here removes
    that trap.

    Returns JSON string with results.
    """
    if store is None:
        return tool_error(
            "Memory is not available. It may be disabled in config or this environment.",
            success=False,
        )

    # Accept new_text as an alias for content (single-op path). See docstring.
    if content is None and new_text is not None:
        content = new_text

    # Accept new_text as an alias for content (single-op path). See docstring.
    if content is None and new_text is not None:
        content = new_text

    # Some strict providers fill optional schema fields with JSON null rather
    # than omitting them.  Treat ``target: null`` as omitted so memory writes
    # still use the documented default store instead of failing validation.
    if target is None:
        target = "memory"

    bg_gate = _background_delete_gate(action, operations, target=target, content=content, old_text=old_text)
    if bg_gate is not None:
        return bg_gate
    target_error = _memory_target_error(store, target)
    if target_error is not None:
        return json.dumps(target_error)

    # search is a read-only retrieval path — no gate, no required content.
    if action == "search":
        rows = store.search(
            target,
            source_filter=source_filter,
            min_trust=min_trust,
            include_superseded=bool(include_superseded),
        )
        return json.dumps(
            {
                "success": True,
                "target": target,
                "results": rows,
                "result_count": len(rows),
            },
            ensure_ascii=False,
        )

    if action == "compact":
        prefer_param = prefer if prefer is not None else "longest"
        try:
            target_size_int = int(target_size) if target_size is not None else None
        except (TypeError, ValueError):
            return tool_error(
                "target_size must be an integer number of characters.", success=False
            )
        result = store.compact(target, target_size=target_size_int, prefer=prefer_param)
        return json.dumps(result, ensure_ascii=False)

    # --- Batch path -------------------------------------------------------
    if operations:
        if not isinstance(operations, list):
            return tool_error(
                "operations must be a list of {action, content?, old_text?} objects.",
                success=False,
            )
        gate_result = _apply_batch_write_gate(target, operations)
        if gate_result is not None:
            return gate_result
        result = store.apply_batch(
            target, operations, memory_char_limit=memory_char_limit
        )
        return json.dumps(result, ensure_ascii=False)

    # --- Single-op path ---------------------------------------------------
    # Validate required params BEFORE the gate so an invalid write is rejected
    # immediately instead of being staged and only failing at approve time.
    if action == "add" and not content:
        return tool_error("Content is required for 'add' action.", success=False)
    if action == "replace" and (not old_text or not content):
        missing = "old_text" if not old_text else "content"
        if not old_text:
            # The client/model omitted old_text. Replace is inherently targeted
            # -- we can't guess which entry. Return the current inventory plus a
            # retry instruction so the model can reissue with old_text set,
            # instead of hitting a dead-end error. (issues #43412, #49466)
            return _missing_old_text_error(store, target, "replace")
        return tool_error(f"{missing} is required for 'replace' action.", success=False)
    if action == "remove" and not old_text:
        return _missing_old_text_error(store, target, "remove")

    # Approval gate: when on, stages the write (background/gateway) or prompts
    # inline (interactive CLI); when off (default) passes straight through.
    gate_result = _apply_write_gate(
        action,
        target,
        content,
        old_text,
        source_class=source_class,
        trust_tier=trust_tier,
    )
    if gate_result is not None:
        return gate_result

    if action == "add":
        try:
            result = store.add(
                target, content, source_class=source_class, trust_tier=trust_tier
            )
        except Exception as exc:
            return _memory_enriched_error(exc, "add")

    elif action == "replace":
        try:
            result = store.replace(
                target,
                old_text,
                content,
                source_class=source_class,
                trust_tier=trust_tier,
            )
        except Exception as exc:
            return _memory_enriched_error(exc, "replace")

    elif action == "remove":
        try:
            result = store.remove(target, old_text)
        except Exception as exc:
            return _memory_enriched_error(exc, "remove")

    elif action == "supersede":
        try:
            result = store.supersede(target, old_text)
        except Exception as exc:
            return _memory_enriched_error(exc, "supersede")

    else:
        return tool_error(
            f"Unknown action '{action}'. Use: add, replace, remove, search, supersede",
            success=False,
        )

    return json.dumps(result, ensure_ascii=False)


def get_builtin_memory_config(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return a normalized built-in memory config mapping.

    Missing, unreadable, or malformed sections become an empty mapping, whose
    missing flags resolve to the enabled defaults. ``agent_init`` consumes this
    same normalized section so tool availability and store construction cannot
    diverge.
    """
    if config is None:
        try:
            from hermes_cli.config import load_config_readonly

            config = load_config_readonly()
        except Exception:
            logger.debug("Could not read memory config for availability", exc_info=True)
            return {}

    section = config.get("memory") if isinstance(config, dict) else None
    return section if isinstance(section, dict) else {}


def get_builtin_memory_store_flags(config: Optional[Dict[str, Any]] = None) -> Tuple[bool, bool]:
    """Return ``(memory_enabled, user_profile_enabled)`` from resolved config."""
    section = get_builtin_memory_config(config)
    return (
        is_truthy_value(section.get("memory_enabled"), default=True),
        is_truthy_value(section.get("user_profile_enabled"), default=True),
    )


@no_cache_check_fn
def check_memory_requirements() -> bool:
    """Snapshot store flags and report whether the built-in tool is available."""
    _memory_surface_flags.set(None)
    flags = get_builtin_memory_store_flags()
    _memory_surface_flags.set(flags)
    return flags[0] or flags[1]


def _memory_target_error(store: "MemoryStore", target: str) -> Optional[Dict[str, Any]]:
    """Return a shared validation error for an invalid or disabled target."""
    if target not in {"memory", "user"}:
        from tools.registry import _bound_error_text

        return {
            "success": False,
            "error": _bound_error_text(
                f"Invalid memory target '{target}'. Use 'memory' or 'user'."
            ),
        }
    if store.target_enabled(target):
        return None
    label = "USER.md" if target == "user" else "MEMORY.md"
    return {
        "success": False,
        "error": f"Built-in {label} writes are disabled in memory config.",
        "target": target,
    }


def apply_memory_pending(
    payload: Dict[str, Any], store: "MemoryStore"
) -> Dict[str, Any]:
    """Replay a staged memory write directly against the store, bypassing the
    write gate. Called by the /memory approve handler.

    Returns the store's result dict.
    """
    action = payload.get("action")
    target = payload.get("target", "memory")
    target_error = _memory_target_error(store, target)
    if target_error is not None:
        return target_error
    content = payload.get("content") or ""
    old_text = payload.get("old_text") or ""
    source_class = payload.get("source_class", DEFAULT_SOURCE_CLASS)
    trust_tier = payload.get("trust_tier", DEFAULT_TRUST_TIER)
    if action == "batch":
        return store.apply_batch(target, payload.get("operations") or [])
    if action == "add":
        return store.add(
            target, content, source_class=source_class, trust_tier=trust_tier
        )
    if action == "replace":
        return store.replace(
            target, old_text, content, source_class=source_class, trust_tier=trust_tier
        )
    if action == "remove":
        return store.remove(target, old_text)
    return {"success": False, "error": f"Unknown staged action '{action}'."}


# OpenAI Function-Calling Schema
# =============================================================================

MEMORY_SCHEMA = {
    "name": "memory",
    "description": (
        "Save durable facts to persistent memory that survive across sessions. Memory is "
        "injected into every future turn, so keep entries compact and high-signal.\n\n"
        "HOW: make ALL your changes in ONE call via an 'operations' array (each item: "
        "{action, content?, old_text?}). The batch applies atomically and the char limit is "
        "checked only on the FINAL result — so a single call can remove/replace stale entries "
        "to free room AND add new ones, even when an add alone would overflow. The response "
        "reports current/limit chars and confirms completion; one batch call finishes the "
        "update, so don't repeat it. Use the bare action/content/old_text fields only for a "
        "single lone change. Use action='search' to read entries back (optionally filtered "
        "by provenance via source_filter / min_trust).\n\n"
        "WHEN: save proactively when the user states a preference, correction, or personal "
        "detail, or you learn a stable fact about their environment, conventions, or workflow. "
        "Priority: user preferences & corrections > environment facts > procedures. The best "
        "memory stops the user repeating themselves.\n\n"
        "IF FULL: an add is auto-accepted by evicting the OLDEST entry to make room "
        "(the evicted text is listed under `evicted` in the response, so nothing is lost silently). "
        "Only if the new entry alone exceeds the whole budget, or the safety floor is reached, is the add rejected with the current entries shown — then reissue as ONE batch that removes or shortens enough stale entries and adds the new one together. Or call action='compact' first to shorten entries so the batch fits.\n\n"
        "TARGETS: 'user' = who the user is (name, role, preferences, style). 'memory' = your "
        "notes (environment, conventions, tool quirks, lessons).\n\n"
        "PROVENANCE (optional, on add/replace): tag where a fact came from. source_class = "
        "user_input (the user told you), external_tool (a tool/API returned it), agent_authored "
        "(YOUR own inference — treat as a guess), or system. trust_tier rates reliability. "
        "Tagging agent_authored guesses keeps them distinguishable from facts the user "
        "actually stated.\n\n"
        "SKIP: trivial/obvious info, easily re-discovered facts, raw data dumps, task progress, "
        "completed-work logs, temporary TODO state (use session_search for those). Reusable "
        "procedures belong in a skill, not memory."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["add", "replace", "remove", "search", "compact", "supersede"],
                "description": "The action to perform (single op, 'search' to read entries, 'compact' to shorten entries to fit, or 'supersede' to retire a stale entry — hidden from recall, kept on disk). Omit when using the 'operations' batch array.",
            },
            "target": {
                "type": "string",
                "enum": ["memory", "user"],
                "description": "Which memory store: 'memory' for personal notes, 'user' for user profile.",
            },
            "content": {
                "type": "string",
                "description": "The entry content. Required for 'add' and 'replace' (single-op shape). Alias: 'new_text' is also accepted (mirrors old_text)."
            },
            "old_text": {
                "type": "string",
                "description": "REQUIRED for 'replace' and 'remove' (single-op shape): a short unique substring identifying the existing entry to modify. Omit only for 'add'.",
            },
            "new_text": {
                "type": "string",
                "description": "Alias for 'content' (single-op shape). Provided so the replace/remove old_text/new_text pairing works; if both are set, 'content' wins."
            },
            "new_text": {
                "type": "string",
                "description": "Alias for 'content' (single-op shape). Provided so the replace/remove old_text/new_text pairing works; if both are set, 'content' wins."
            },
            "operations": {
                "type": "array",
                "description": (
                    "Batch shape: a list of operations applied atomically in one call "
                    "against the final char budget. Preferred when making multiple changes "
                    "or consolidating to make room. Each item is {action, content?, old_text?}."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["add", "replace", "remove"]},
                        "content": {"type": "string", "description": "Entry content for add/replace. Alias: 'new_text'."},
                        "new_text": {"type": "string", "description": "Alias for 'content' in a batch op."},
                        "old_text": {"type": "string", "description": "Substring identifying the entry for replace/remove."},
                    },
                    "required": ["action"],
                },
            },
            "target_size": {
                "type": "integer",
                "description": "Optional for 'compact': target character count (defaults to the store limit).",
            },
            "prefer": {
                "type": "string",
                "enum": ["longest", "oldest"],
                "description": "Optional for 'compact': which entries to trim first (default: longest).",
            },
            "source_class": {
                "type": "string",
                "enum": list(SOURCE_CLASSES),
                "description": (
                    "Optional provenance for 'add'/'replace': who produced this fact. "
                    "Defaults to 'unknown'. Use 'agent_authored' for your own guesses."
                ),
            },
            "trust_tier": {
                "type": "string",
                "enum": list(TRUST_TIERS),
                "description": (
                    "Optional provenance for 'add'/'replace': how reliable this fact is. "
                    "Defaults to 'unknown'."
                ),
            },
            "source_filter": {
                "type": "array",
                "items": {"type": "string", "enum": list(SOURCE_CLASSES)},
                "description": "Optional for 'search': keep only entries with these source classes.",
            },
            "min_trust": {
                "type": "string",
                "enum": list(TRUST_TIERS),
                "description": "Optional for 'search': keep only entries at or above this trust tier.",
            },
            "include_superseded": {
                "type": "boolean",
                "description": "Optional for 'search': also return superseded (retired) entries, each with a superseded_at timestamp. Default false.",
            },
            "memory_char_limit": {
                "type": "integer",
                "description": (
                    "Optional per-batch override for the 'memory' target char limit, "
                    "only honoured when config 'memory.allow_batch_memory_char_limit_override' "
                    "is True. Ignored for 'user' target. The system-prompt snapshot always "
                    "uses the configured limit (issue #517)."
                ),
            },
        },
        "required": ["target"],
    },
}


def _build_memory_schema_overrides() -> Dict[str, Any]:
    """Narrow the advertised target surface using the availability snapshot."""
    flags = _memory_surface_flags.get()
    _memory_surface_flags.set(None)
    if flags is None:
        flags = get_builtin_memory_store_flags()
    memory_enabled, user_profile_enabled = flags
    targets = []
    if memory_enabled:
        targets.append("memory")
    if user_profile_enabled:
        targets.append("user")

    parameters = copy.deepcopy(MEMORY_SCHEMA["parameters"])
    target_schema = parameters["properties"]["target"]
    target_schema["enum"] = targets

    description = MEMORY_SCHEMA["description"]
    if targets == ["memory"]:
        target_schema["description"] = "The enabled built-in store: 'memory' for personal notes."
        description = description.replace(
            "TARGETS: 'user' = who the user is (name, role, preferences, style). 'memory' = your "
            "notes (environment, conventions, tool quirks, lessons).",
            "TARGET: only 'memory' is enabled for personal notes (environment, conventions, "
            "tool quirks, lessons).",
        )
    elif targets == ["user"]:
        target_schema["description"] = "The enabled built-in store: 'user' for user profile."
        description = description.replace(
            "TARGETS: 'user' = who the user is (name, role, preferences, style). 'memory' = your "
            "notes (environment, conventions, tool quirks, lessons).",
            "TARGET: only 'user' is enabled for user profile facts (name, role, preferences, style).",
        )

    return {"description": description, "parameters": parameters}


# --- Registry ---
from tools.registry import registry, tool_error

registry.register(
    name="memory",
    toolset="memory",
    schema=MEMORY_SCHEMA,
    handler=lambda args, **kw: memory_tool(
        action=args.get("action", ""),
        target=args.get("target", "memory"),
        content=args.get("content"),
        old_text=args.get("old_text"),
        new_text=args.get("new_text"),
        source_class=args.get("source_class", DEFAULT_SOURCE_CLASS),
        trust_tier=args.get("trust_tier", DEFAULT_TRUST_TIER),
        source_filter=args.get("source_filter"),
        min_trust=args.get("min_trust"),
        include_superseded=args.get("include_superseded"),
        operations=args.get("operations"),
        target_size=args.get("target_size"),
        prefer=args.get("prefer"),
        memory_char_limit=args.get("memory_char_limit"),
        store=kw.get("store"),
    ),
    check_fn=check_memory_requirements,
    emoji="🧠",
    dynamic_schema_overrides=_build_memory_schema_overrides,
)

# ---- BEGIN PLUGIN-COMPAT (revert-scheduled; see COMPAT_MANIFEST.md) ----
from contextlib import contextmanager  # noqa: F401,E402
import time  # noqa: F401,E402

_PLUGIN_COMPAT_LAZY = {
    'atomic_write_text': ('utils', 'atomic_write_text'),
}

def __getattr__(name):  # PEP 562 — lazy so no import cycles
    target = _PLUGIN_COMPAT_LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib
    from hermes_cli.plugin_compat import warn_once
    warn_once(__name__, name, *target)
    return getattr(importlib.import_module(target[0]), target[1])
# ---- END PLUGIN-COMPAT ----
