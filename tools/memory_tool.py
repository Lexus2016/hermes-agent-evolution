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


def _pin_matched_entries(store: "MemoryStore", payload: Dict[str, Any]) -> Optional[str]:
    """Record on each staged replace/remove the FULL entry its old_text selects now. Approval
    then applies to exactly the entry the approver reviewed and refuses if it changed:
    re-running the old_text search at approve time could hit a newer entry that still
    contains it. Returns the JSON error when the search fails now, as the direct write would."""
    target = payload.get("target", "memory")
    if payload.get("action") == "batch":
        result = store.resolve_batch_entries(target, payload["operations"])
        if result.get("success"):
            payload["operations"] = [op if entry is None else {**op, "matched_entry": entry}
                                     for op, entry in zip(payload["operations"], result["matched_entries"])]
    elif payload.get("action") in _BG_DELETE_ACTIONS:
        result = store.resolve_entry(target, payload.get("old_text") or "", payload["action"])
        if result.get("success"):
            payload["matched_entry"] = result["matched_entry"]
    else:
        return None
    return None if result.get("success") else json.dumps(result, ensure_ascii=False)


def _gate_or_stage(store: "MemoryStore", summary: str, detail: str, payload: Dict[str, Any]) -> Optional[str]:
    """JSON tool-result string when the write must NOT proceed (blocked or staged
    for approval), None to proceed. Fails open if the gate module can't load."""
    try:
        from tools import write_approval as wa
    except Exception:
        return None
    decision = wa.evaluate_gate(wa.MEMORY, inline_summary=summary, inline_detail=detail)
    if decision.allow:
        return None
    if decision.blocked:
        return tool_error(decision.message, success=False)
    if (unmatched := _pin_matched_entries(store, payload)) is not None:
        return unmatched
    record = wa.stage_write(wa.MEMORY, payload, summary=f"{summary}: {detail[:120]}", origin=wa.current_origin())
    return json.dumps({"success": True, "staged": True, "pending_id": record["id"], "message": decision.message},
                      ensure_ascii=False)


# action -> (store call, gate (summary, detail) text) for the live tool path and staged replay.
# Provenance (#316) rides on add/replace; ``entry`` is the pinned matched_entry for replay.
_STORE_ACTIONS = {
    "add": (lambda store, target, content, old_text, entry=None, source_class=DEFAULT_SOURCE_CLASS, trust_tier=DEFAULT_TRUST_TIER:
            store.add(target, content, source_class=source_class, trust_tier=trust_tier),
            lambda label, content, old_text: (f"add to {label}", content or "")),
    "replace": (lambda store, target, content, old_text, entry=None, source_class=DEFAULT_SOURCE_CLASS, trust_tier=DEFAULT_TRUST_TIER:
                store.replace(target, old_text, content, source_class=source_class, trust_tier=trust_tier, matched_entry=entry),
                lambda label, content, old_text: (f"replace in {label}",
                                                  f"entry matching: {old_text}\nwhole entry becomes: {content}")),
    "remove": (lambda store, target, content, old_text, entry=None, source_class=DEFAULT_SOURCE_CLASS, trust_tier=DEFAULT_TRUST_TIER:
               store.remove(target, old_text, matched_entry=entry),
               lambda label, content, old_text: (f"remove from {label}", old_text or ""))}


def _batch_op_line(op: Dict[str, Any]) -> str:
    op = op or {}
    act, content, old = op.get("action", "?"), op.get("content") or op.get("new_text") or "", op.get("old_text", "")
    if act == "remove":
        return f"- remove: {old}"
    # Whole-entry contract (#117952): the approver must not read this as a span patch.
    return (f"- replace entry matching '{old}' -> whole entry becomes: {content}" if act == "replace"
            else f"- {act}: {content}")


def _apply_write_gate(store: "MemoryStore", action: str, target: str, content: Optional[str],
                      old_text: Optional[str], operations: Optional[List[Dict[str, Any]]] = None,
                      source_class: str = DEFAULT_SOURCE_CLASS,
                      trust_tier: str = DEFAULT_TRUST_TIER) -> Optional[str]:
    """Gate one mutating op, or (``operations`` set) a whole batch as a single unit.

    Provenance tags (#316) ride in the staged payload so an approved write keeps them.
    """
    label = "user profile" if target == "user" else "memory"
    if operations is not None:
        return _gate_or_stage(store, f"apply {len(operations)} op(s) to {label}",
                              "\n".join(_batch_op_line(op) for op in operations),
                              {"action": "batch", "target": target, "operations": operations,
                               "source_class": source_class, "trust_tier": trust_tier})
    return _gate_or_stage(store, *_STORE_ACTIONS[action][1](label, content, old_text),
                          {"action": action, "target": target, "content": content, "old_text": old_text,
                           "source_class": source_class, "trust_tier": trust_tier})


def _validate_single_op(store, action, target, content, old_text) -> Optional[str]:
    """Validate BEFORE the gate so an invalid write is rejected now, not at approve time.
    Missing ``old_text`` is recoverable (it can't be schema-required — needs a combinator
    the Codex backend rejects): return the inventory plus a retry instruction."""
    if action == "add" and not content:
        return tool_error("Content is required for 'add' action.", success=False)
    if action in ("replace", "remove") and not old_text:
        replace_hint = (" For 'replace', content is the COMPLETE new entry -- the whole "
                        "matched entry is overwritten, not just the old_text span."
                        if action == "replace" else "")
        return json.dumps({
            "success": False,
            "error": (f"'{action}' needs old_text -- a short unique substring of the entry "
                      f"to {action}. None was provided. Reissue the {action} with old_text "
                      f"set to part of one of the current_entries below.{replace_hint}"),
            "current_entries": store._entries_for(target), "usage": store._usage(target)}, ensure_ascii=False)
    if action == "replace" and not content:
        return tool_error("content is required for 'replace' action.", success=False)
    return None


_BG_DELETE_ACTIONS = ("replace", "remove")


def destructive_ops(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The replace/remove ops of a staged memory payload, single-op or batch shape."""
    ops = (payload.get("operations") or []) if payload.get("action") == "batch" else [payload]
    return [op for op in ops if (op or {}).get("action") in _BG_DELETE_ACTIONS]


def _background_delete_gate(store, action, operations, target="memory", content=None,
                            old_text=None) -> Optional[str]:
    """Fail-closed operation gate for unattended background-review forks (#105921): ``add``
    stays available (it is all any review prompt asks for), while ``replace``/``remove`` —
    single or inside a batch — are never applied unattended. The op is staged in the pending
    store instead of merely denied: the fork's own review summary is never published back, so
    a plain denial would drop the consolidation request with no surfacing path at all. A
    staging failure fails closed to a plain denial."""
    from tools.skill_provenance import is_unattended_review

    if not is_unattended_review():
        return None
    payload = ({"action": "batch", "target": target, "operations": operations}
               if operations is not None else
               {"action": action, "target": target, "content": content, "old_text": old_text})
    if not destructive_ops(payload):
        return None
    detail = ("; ".join(_batch_op_line(op) for op in operations) if operations is not None
              else _batch_op_line({"action": action, "content": content, "old_text": old_text}))
    try:
        if (unmatched := _pin_matched_entries(store, payload)) is not None:
            return unmatched
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

    target_error = _memory_target_error(store, target)
    if target_error is not None:
        return json.dumps(target_error)
    bg_gate = _background_delete_gate(store, action, operations, target=target, content=content, old_text=old_text)
    if bg_gate is not None:
        return bg_gate

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
        gate_result = _apply_write_gate(
            store, "batch", target, None, None, operations,
            source_class=source_class, trust_tier=trust_tier,
        )
        if gate_result is not None:
            return gate_result
        try:
            result = store.apply_batch(target, operations, memory_char_limit=memory_char_limit)
        except Exception as exc:
            return _memory_enriched_error(exc, "batch")
        return json.dumps(result, ensure_ascii=False)

    if action in ("add", "replace", "remove"):
        invalid = _validate_single_op(store, action, target, content, old_text)
        if invalid is not None:
            return invalid
        gate_result = _apply_write_gate(
            store, action, target, content, old_text,
            source_class=source_class, trust_tier=trust_tier,
        )
        if gate_result is not None:
            return gate_result
        try:
            result = _STORE_ACTIONS[action][0](
                store, target, content, old_text,
                source_class=source_class, trust_tier=trust_tier,
            )
        except Exception as exc:
            return _memory_enriched_error(exc, action)
        return json.dumps(result, ensure_ascii=False)

    if action == "supersede":
        try:
            result = store.supersede(target, old_text)
        except Exception as exc:
            return _memory_enriched_error(exc, "supersede")
        return json.dumps(result, ensure_ascii=False)

    return tool_error(
        f"Unknown action '{action}'. Use: add, replace, remove, search, compact, supersede",
        success=False,
    )


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



def apply_memory_pending(payload: Dict[str, Any], store: "MemoryStore") -> Dict[str, Any]:
    """Replay a staged write against the store, bypassing the gate (/memory approve).

    A replace/remove applies to exactly its pinned ``matched_entry`` or is refused; a record
    staged before pinning has no verifiable target, so it is refused rather than replayed by
    old_text (which could hit a newer entry the approver never saw). Provenance tags from the
    staged payload are preserved (#316).
    """
    action, target = payload.get("action"), payload.get("target", "memory")
    target_error = _memory_target_error(store, target)
    if target_error is not None:
        return target_error
    if any(not op.get("matched_entry") for op in destructive_ops(payload)):
        return {"success": False, "error": "This destructive pending write predates entry pinning and cannot be "
                                           "verified; nothing was applied. Reject it and recreate the change."}
    content = payload.get("content") or ""
    old_text = payload.get("old_text") or ""
    source_class = payload.get("source_class", DEFAULT_SOURCE_CLASS)
    trust_tier = payload.get("trust_tier", DEFAULT_TRUST_TIER)
    if action == "batch":
        return store.apply_batch(target, payload.get("operations") or [])
    if action == "supersede":
        return store.supersede(target, old_text)
    if action not in _STORE_ACTIONS:
        return {"success": False, "error": f"Unknown staged action '{action}'."}
    return _STORE_ACTIONS[action][0](
        store, target, content, old_text, payload.get("matched_entry"),
        source_class=source_class, trust_tier=trust_tier,
    )


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
                "description": "The entry content. Required for 'add' and 'replace'. For 'replace' it is the COMPLETE new entry text: the whole matched entry is overwritten, so include everything you want to keep. Alias: 'new_text' is also accepted (same full-entry meaning)."
            },
            "old_text": {
                "type": "string",
                "description": "REQUIRED for 'replace' and 'remove' (single-op shape): a short unique substring IDENTIFYING the existing entry to modify -- it locates the entry, it is not spliced out. Omit only for 'add'."
            },
            "new_text": {
                "type": "string",
                "description": "Alias for 'content' (single-op shape): the COMPLETE new entry for 'replace', not a patch of old_text. If both are set, 'content' wins."
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
                        "content": {"type": "string", "description": "Entry content for add/replace. For replace, the COMPLETE new entry (whole entry is overwritten). Alias: 'new_text'."},
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
