"""Model-identity metadata + model-aware retrieval filtering for tqmemory (#2234).

Thin interceptor — does NOT touch the tqmemory MCP server. Both write-stamp
and read-filter live in invoke_tool (the single chokepoint with agent.model).
"""

import json
import logging
from typing import Any, Optional

from agent.adversarial_verification import detect_model_family

logger = logging.getLogger(__name__)

_CROSS_FAMILY_PENALTY = 0.5

_WRITE_TOOLS = frozenset({"mcp_tqmemory_remember_note", "mcp__tqmemory__remember_note"})
_READ_TOOLS = frozenset({
    "mcp_tqmemory_recent_context", "mcp_tqmemory_semantic_search",
    "mcp__tqmemory__recent_context", "mcp__tqmemory__semantic_search",
})


def is_tqmemory_write(name: str) -> bool:
    return name in _WRITE_TOOLS


def is_tqmemory_read(name: str) -> bool:
    return name in _READ_TOOLS


def stamp_model_metadata(args: dict, model: Optional[str]) -> dict:
    """Return *args* with model_identity/model_family in metadata (#2234). Never raises."""
    try:
        result = dict(args) if isinstance(args, dict) else {}
        meta = dict(result.get("metadata") or {})
        meta["model_identity"] = (model or "").strip()
        meta["model_family"] = detect_model_family(model)
        result["metadata"] = meta
        return result
    except Exception:
        return args


def filter_by_model_family(
    result_json: str, consumer_model: Optional[str], *, penalty: float = _CROSS_FAMILY_PENALTY,
) -> str:
    """Down-weight cross-family notes in a tqmemory retrieval result (#2234).

    Entries whose model_family differs from the consumer's get their relevance
    multiplied by *penalty* (default 0.5), then entries are re-sorted by
    relevance descending. Entries without metadata or an unknown consumer are
    left untouched. Never raises — returns *result_json* unchanged on any error.
    """
    try:
        data = json.loads(result_json)
    except (json.JSONDecodeError, TypeError):
        return result_json

    consumer_family = detect_model_family(consumer_model)
    if consumer_family == "unknown":
        return result_json

    entries = None
    for key in ("notes", "entries", "results", "context"):
        val = data.get(key) if isinstance(data, dict) else None
        if isinstance(val, list):
            entries = val
            break
    if entries is None:
        return result_json

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        meta = entry.get("metadata") or {}
        ef = meta.get("model_family") if isinstance(meta, dict) else None
        if ef in ("", None, "unknown") or ef == consumer_family:
            continue
        entry["relevance"] = round(float(entry.get("relevance", 1.0)) * penalty, 4)

    entries.sort(key=lambda e: float(e.get("relevance", 0.0)) if isinstance(e, dict) else 0.0, reverse=True)
    try:
        return json.dumps(data, ensure_ascii=False)
    except (TypeError, ValueError):
        return result_json
