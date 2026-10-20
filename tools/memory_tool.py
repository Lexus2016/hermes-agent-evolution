#!/usr/bin/env python3
"""Memory Tool - persistent curated memory (MEMORY.md = agent notes, USER.md = user
profile). Both enter the system prompt as a FROZEN snapshot at session start;
mid-session writes hit disk but never change the prompt (prefix cache intact).
Single `memory` tool: add/patch/remove or a batch `operations` list."""

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

# One tool-definition pass must use ONE config decision for availability and the
# dynamic target schema: the check_fn result flows to the immediately following
# dynamic_schema_overrides call; ContextVar isolates concurrent profile builds.
_memory_surface_flags: ContextVar[Optional[tuple[bool, bool]]] = ContextVar("memory_surface_flags", default=None)

def get_memory_dir() -> Path:
    """Return the profile-scoped memories directory."""
    return get_hermes_home() / "memories"

from tools.memory_tool_store import (
    BLOCKED_WARNINGS_FILE,
    BLOCKED_WARNINGS_MAX,
    DEFAULT_MEMORY_CHAR_LIMIT,
    DEFAULT_SOURCE_CLASS,
    DEFAULT_TRUST_TIER,
    DEFAULT_USER_CHAR_LIMIT,
    ENTRY_DELIMITER,
    FAILURE_CLASS,
    MEMORY_BLOCK_HEADERS,
    PATCH_DROP_HINT,
    MemoryStore,
    SOURCE_CLASSES,
    TRUST_TIERS,
    _PROV_CLOSE,
    _PROV_OPEN,
    _drift_error,
    _log_guard_event,
    _make_provenance,
    _read_failed_error,
    _scan_memory_content,
    _trust_rank,
    _validate_provenance,
    encode_provenance,
    parse_provenance,
)

def load_on_disk_store() -> MemoryStore:
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


def _pin_matched_entries(store: MemoryStore, payload: dict[str, Any]) -> Optional[str]:
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


def _gate_or_stage(store: MemoryStore, summary: str, detail: str, payload: dict[str, Any]) -> Optional[str]:
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
        FAILURE_CLASS.set("gate_refused")
        return tool_error(decision.message, success=False)
    if (unmatched := _pin_matched_entries(store, payload)) is not None:
        return unmatched
    FAILURE_CLASS.set("staged")
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
    "remove": (lambda store, target, content, old_text, entry=None, **kwargs:
               store.remove(target, old_text, matched_entry=entry),
               lambda label, content, old_text: (f"remove from {label}", old_text or "")),
    "patch": (lambda store, target, content, old_text, entry=None, **kwargs:
              store.patch(target, old_text, content, matched_entry=entry),
              lambda label, content, old_text: (f"patch in {label}",
                                                f"entry matching: {old_text}\nspan '{old_text}' becomes: {content} "
                                                "(rest of the entry is kept)")),
}


def _batch_op_line(op: dict[str, Any]) -> str:
    op = op or {}
    act, content, old = op.get("action", "?"), op.get("content") or op.get("new_text") or "", op.get("old_text", "")
    if act == "remove":
        return f"- remove: {old}"
    if act == "patch":
        return f"- patch span '{old}' -> '{content}' (rest of the entry is kept)"
    return f"- {act}: {content}"


def _apply_write_gate(store: MemoryStore, action: str, target: str, content: Optional[str],
                      old_text: Optional[str], operations: Optional[list[dict[str, Any]]] = None,
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
        FAILURE_CLASS.set("missing_content")
        return tool_error("Content is required for 'add' action.", success=False)
    if action in ("remove", "patch") and not old_text:
        FAILURE_CLASS.set("missing_old_text")
        return json.dumps({
            "success": False,
            "error": (f"'{action}' needs old_text -- a short unique substring of the entry "
                      f"to {action}. None was provided. Reissue the {action} with old_text "
                      f"set to part of one of the current_entries below."),
            "current_entries": store._entries_for(target), "usage": store._usage(target)}, ensure_ascii=False)
    if action == "patch" and not content:
        FAILURE_CLASS.set("missing_content")
        return tool_error(f"content (the new text for the old_text span) is required for 'patch' action. "
                          f"{PATCH_DROP_HINT}", success=False)
    return None


# 'replace' is no longer offered to the model; it stays here so writes staged before its
# removal still pin, display and replay.
_BG_DELETE_ACTIONS = ("replace", "remove", "patch")
_REPLACE_REMOVED = ("'replace' is not available; use 'patch'. old_text is the exact span to change and "
                    "content is its new text; the rest of the entry is kept. To rewrite a whole entry, "
                    "set old_text to the whole entry. Nothing was written.")


def destructive_ops(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """The replace/remove ops of a staged memory payload, single-op or batch shape."""
    ops = (payload.get("operations") or []) if payload.get("action") == "batch" else [payload]
    return [op for op in ops if (op or {}).get("action") in _BG_DELETE_ACTIONS]


def _background_delete_gate(store, action, operations, target="memory", content=None,
                            old_text=None) -> Optional[str]:
    """Fail-closed operation gate for unattended background-review forks (#105921): ``add``
    stays available (it is all any review prompt asks for), while ``patch``/``remove`` —
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
        FAILURE_CLASS.set("staged")
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
        FAILURE_CLASS.set("gate_refused")
        return tool_error(
            "Background review may not delete memory entries ('patch'/'remove', including in a "
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


def _applied(result: dict[str, Any]) -> tuple[str, str]:
    return ("success" if result.get("success") else "failed"), json.dumps(result, ensure_ascii=False)


def _invalid(message: str) -> tuple[str, str]:
    FAILURE_CLASS.set("invalid_args")
    return "rejected", tool_error(message, success=False)


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
    operations: Optional[list[dict[str, Any]]] = None,
    target_size: Optional[int] = None,
    prefer: str = "longest",
    memory_char_limit: Optional[int] = None,
    store: Optional[MemoryStore] = None,
) -> str:
    """Single entry point for the memory tool. Dispatches to MemoryStore methods."""
    if store is None:
        return tool_error(
            "Memory is not available. It may be disabled in config or this environment.",
            success=False,
        )

    token = FAILURE_CLASS.set("other")
    try:
        outcome, result = _memory_tool(
            action=action,
            target=target,
            content=content,
            old_text=old_text,
            new_text=new_text,
            source_class=source_class,
            trust_tier=trust_tier,
            source_filter=source_filter,
            min_trust=min_trust,
            include_superseded=include_superseded,
            operations=operations,
            target_size=target_size,
            prefer=prefer,
            memory_char_limit=memory_char_limit,
            store=store,
        )
        failure_class = "none" if outcome == "success" else FAILURE_CLASS.get()
    finally:
        FAILURE_CLASS.reset(token)

    try:
        from hermes_cli.observability.shared_metrics_loop import record_builtin_memory_call
        record_builtin_memory_call(action, operations, outcome=outcome, failure_class=failure_class)
    except Exception:
        pass

    return result


def _memory_tool(
    action: Optional[str],
    target: Optional[str],
    content: Optional[str],
    old_text: Optional[str],
    new_text: Optional[str],
    source_class: str,
    trust_tier: str,
    source_filter: Optional[object],
    min_trust: Optional[str],
    include_superseded: bool,
    operations: Optional[list[dict[str, Any]]],
    target_size: Optional[int],
    prefer: str,
    memory_char_limit: Optional[int],
    store: MemoryStore,
) -> tuple[str, str]:
    """Internal memory dispatch returning (outcome, result_json)."""
    # Accept new_text as alias for content
    if not content and new_text:
        content = new_text

    if target is None:
        target = "memory"

    target_error = _memory_target_error(store, target, operations)
    if target_error is not None:
        return "rejected", json.dumps(target_error)

    bg_gate = _background_delete_gate(store, action, operations, target=target, content=content, old_text=old_text)
    if bg_gate is not None:
        return "rejected", bg_gate

    if action == "search":
        rows = store.search(
            target,
            source_filter=source_filter,
            min_trust=min_trust,
            include_superseded=bool(include_superseded),
        )
        return "success", json.dumps(
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
            return _invalid("target_size must be an integer number of characters.")
        result = store.compact(target, target_size=target_size_int, prefer=prefer_param)
        return _applied(result)

    if operations:
        if not isinstance(operations, list):
            return _invalid("operations must be a list of {action, content?, old_text?} objects.")
        gate_result = _apply_write_gate(
            store, "batch", target, None, None, operations,
            source_class=source_class, trust_tier=trust_tier,
        )
        if gate_result is not None:
            return "rejected", gate_result
        try:
            result = store.apply_batch(target, operations, memory_char_limit=memory_char_limit)
        except Exception as exc:
            return "failed", _memory_enriched_error(exc, "batch")
        return _applied(result)

    if not action and not operations:
        return _invalid(
            "Missing required parameter: provide 'action' (add/patch/replace/remove) "
            "or 'operations' (batch list). Got neither."
        )

    if action in ("add", "replace", "patch", "remove"):
        invalid = _validate_single_op(store, action, target, content, old_text)
        if invalid is not None:
            return "rejected", invalid
        gate_result = _apply_write_gate(
            store, action, target, content, old_text,
            source_class=source_class, trust_tier=trust_tier,
        )
        if gate_result is not None:
            return "rejected", gate_result
        try:
            result = _STORE_ACTIONS[action][0](
                store, target, content, old_text,
                source_class=source_class, trust_tier=trust_tier,
            )
        except Exception as exc:
            return "failed", _memory_enriched_error(exc, action)
        return _applied(result)

    if action == "supersede":
        try:
            result = store.supersede(target, old_text)
        except Exception as exc:
            return "failed", _memory_enriched_error(exc, "supersede")
        return _applied(result)

    return _invalid(f"Unknown action '{action}'. Use: add, patch, replace, remove, search, compact, supersede")


def get_builtin_memory_config(config: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Normalized ``memory`` config section ({} when missing/malformed → flags default to
    enabled). ``agent_init`` reads the same section so availability and store cannot diverge."""
    if config is None:
        try:
            from hermes_cli.config import load_config_readonly

            config = load_config_readonly()
        except Exception:
            logger.debug("Could not read memory config for availability", exc_info=True)
            return {}

    section = config.get("memory") if isinstance(config, dict) else None
    return section if isinstance(section, dict) else {}


def get_builtin_memory_store_flags(config: Optional[dict[str, Any]] = None) -> tuple[bool, bool]:
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


def _memory_target_error(
    store: MemoryStore, target: str | None, operations: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Validate one explicit store before writing, staging, or replaying a batch."""
    if target is None:
        FAILURE_CLASS.set("invalid_args")
        return {"success": False, "error": (
            "Missing required target. Retry with target='memory' or target='user' at the top level. "
            "For a batch, remove target from every operation; use separate calls for different stores. "
            "Nothing was written or staged.")}
    if not isinstance(target, str) or target not in {"memory", "user"}:
        from tools.registry import _bound_error_text
        FAILURE_CLASS.set("invalid_args")
        return {"success": False,
                "error": _bound_error_text(f"Invalid memory target '{target}'. Use 'memory' or 'user'.")}
    if isinstance(operations, list):
        for index, op in enumerate(operations, 1):
            # A nested target equal to the call's (or null) is redundant, not wrong; only a
            # different one asks for two stores in one call, which a batch cannot do.
            if isinstance(op, dict) and op.get("target") not in (None, target):
                FAILURE_CLASS.set("invalid_args")
                return {"success": False, "error": (
                    f"Operation {index} names a different target than the one at the top level. "
                    "One call writes one store: retry with separate calls for different stores. "
                    "Nothing was written or staged.")}
    if store.target_enabled(target):
        return None
    label = "USER.md" if target == "user" else "MEMORY.md"
    FAILURE_CLASS.set("disabled")
    return {"success": False, "error": f"Built-in {label} writes are disabled in memory config.", "target": target}


def apply_memory_pending(payload: dict[str, Any], store: MemoryStore) -> dict[str, Any]:
    """Replay a staged write against the store, bypassing the gate (/memory approve). A
    replace/remove applies to exactly its pinned ``matched_entry`` or is refused; a record
    staged before pinning has no verifiable target, so it is refused rather than replayed by
    old_text (which could hit a newer entry the approver never saw). Provenance tags from the
    staged payload are preserved (#316)."""
    action, target = payload.get("action"), payload.get("target", "memory")
    target_error = _memory_target_error(store, target, payload.get("operations"))
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
        "HOW: make ALL your changes to one target in ONE call via an 'operations' array (each "
        "item: {action, content?, old_text?}). The batch applies atomically and the char limit is "
        "checked only on the FINAL result — so a single call can remove/patch stale entries "
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
                "enum": ["add", "patch", "replace", "remove", "search", "compact", "supersede"],
                "description": "The action to perform (single op: 'add', 'patch', 'replace', 'remove', 'search' to read entries, 'compact' to shorten entries to fit, or 'supersede' to retire a stale entry). Omit when using the 'operations' batch array.",
            },
            "target": {
                "type": "string",
                "enum": ["memory", "user"],
                "description": "Which memory store: 'memory' for personal notes, 'user' for user profile.",
            },
            "content": {
                "type": "string",
                "description": "Required for 'add' and 'patch'. For 'add', the new entry. For 'patch', the text that replaces old_text; the rest of the entry is kept. Alias: 'new_text'."
            },
            "old_text": {
                "type": "string",
                "description": "REQUIRED for 'patch' and 'remove': text from one entry. 'patch' replaces exactly this span (it must occur once in the entry); 'remove' deletes the whole entry. Omit for 'add'."
            },
            "new_text": {
                "type": "string",
                "description": "Alias for 'content'. If both are set, 'content' wins."
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
                        "action": {"type": "string", "enum": ["add", "patch", "remove"]},
                        "content": {"type": "string", "description": "Required for add and patch, as 'content' above."},
                        "new_text": {"type": "string", "description": "Alias for 'content' in a batch op."},
                        "old_text": {"type": "string", "description": "As 'old_text' above."},
                    },
                    "required": ["action"],
                    "additionalProperties": False,
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


# Schema text when only one built-in store is enabled: (target description, TARGETS replacement).
_SINGLE_TARGET_TEXT = {
    ("memory",): ("The enabled built-in store: 'memory' for personal notes.",
                  "TARGET: only 'memory' is enabled for personal notes (environment, conventions, "
                  "tool quirks, lessons)."),
    ("user",): ("The enabled built-in store: 'user' for user profile.",
                "TARGET: only 'user' is enabled for user profile facts (name, role, preferences, style).")}


def _build_memory_schema_overrides() -> dict[str, Any]:
    """Narrow the advertised target surface using the availability snapshot."""
    flags = _memory_surface_flags.get() or get_builtin_memory_store_flags()
    _memory_surface_flags.set(None)
    targets = [t for t, on in zip(("memory", "user"), flags) if on]
    parameters = copy.deepcopy(MEMORY_SCHEMA["parameters"])
    target_schema, description = parameters["properties"]["target"], MEMORY_SCHEMA["description"]
    target_schema["enum"] = targets
    if narrowed := _SINGLE_TARGET_TEXT.get(tuple(targets)):
        target_schema["description"], replacement = narrowed
        description = description.replace(
            "TARGETS: 'user' = who the user is (name, role, preferences, style). 'memory' = your "
            "notes (environment, conventions, tool quirks, lessons).", replacement)
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
