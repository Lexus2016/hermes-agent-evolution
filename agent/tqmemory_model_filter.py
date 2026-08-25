"""Model-identity metadata + model-aware & utility-weighted retrieval filtering for tqmemory (#2234, #3199, #3200, #3201).

Thin interceptor — does NOT touch the tqmemory MCP server. Write-stamping
and read-filtering live in invoke_tool / memory orchestration.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from agent.adversarial_verification import detect_model_family

logger = logging.getLogger(__name__)

_CROSS_FAMILY_PENALTY = 0.5

VALID_OUTCOMES = frozenset({"helped", "neutral", "misled"})
OUTCOME_UTILITY_WEIGHTS: Dict[str, float] = {
    "helped": 1.25,
    "neutral": 1.0,
    "misled": 0.2,
}

_WRITE_TOOLS = frozenset({
    "mcp_tqmemory_remember_note",
    "mcp__tqmemory__remember_note",
})
_READ_TOOLS = frozenset({
    "mcp_tqmemory_recent_context",
    "mcp_tqmemory_semantic_search",
    "mcp__tqmemory__recent_context",
    "mcp__tqmemory__semantic_search",
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


def stamp_outcome_metadata(args: dict, outcome: Optional[str] = None) -> dict:
    """Return *args* with validated outcome and utility in metadata (#3199). Never raises."""
    try:
        result = dict(args) if isinstance(args, dict) else {}
        meta = dict(result.get("metadata") or {})
        raw_outcome = outcome if outcome is not None else result.get("outcome")
        if isinstance(raw_outcome, str):
            norm_outcome = raw_outcome.strip().lower()
            if norm_outcome in VALID_OUTCOMES:
                meta["outcome"] = norm_outcome
                meta["utility"] = OUTCOME_UTILITY_WEIGHTS[norm_outcome]
        result["metadata"] = meta
        return result
    except Exception:
        return args


def compute_note_utility(metadata: Optional[dict]) -> float:
    """Extract utility multiplier from note metadata, defaulting to 1.0."""
    if not isinstance(metadata, dict):
        return 1.0
    if "utility" in metadata:
        try:
            return float(metadata["utility"])
        except (ValueError, TypeError):
            pass
    outcome = metadata.get("outcome")
    if isinstance(outcome, str):
        return OUTCOME_UTILITY_WEIGHTS.get(outcome.strip().lower(), 1.0)
    return 1.0


def rank_by_utility_and_family(
    result_json: str,
    consumer_model: Optional[str] = None,
    *,
    cross_family_penalty: float = _CROSS_FAMILY_PENALTY,
) -> str:
    """Rank retrieval results by (relevance x utility x family_penalty) (#3200).

    Notes tagged 'helped' are boosted (1.25x), 'misled' are heavily down-weighted (0.2x),
    and cross-model-family notes receive a cross_family_penalty (0.5x).
    Entries are re-sorted by adjusted relevance descending. Never raises.
    """
    try:
        data = json.loads(result_json)
    except (json.JSONDecodeError, TypeError):
        return result_json

    consumer_family = detect_model_family(consumer_model) if consumer_model else "unknown"

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
        utility = compute_note_utility(meta if isinstance(meta, dict) else None)
        entry["utility"] = utility

        rel = float(entry.get("relevance", 1.0))
        adjusted_rel = rel * utility

        if consumer_family != "unknown" and isinstance(meta, dict):
            ef = meta.get("model_family")
            if ef not in ("", None, "unknown") and ef != consumer_family:
                adjusted_rel *= cross_family_penalty

        entry["relevance"] = round(adjusted_rel, 4)

    entries.sort(
        key=lambda e: float(e.get("relevance", 0.0)) if isinstance(e, dict) else 0.0,
        reverse=True,
    )
    try:
        return json.dumps(data, ensure_ascii=False)
    except (TypeError, ValueError):
        return result_json


def filter_by_model_family(
    result_json: str,
    consumer_model: Optional[str],
    *,
    penalty: float = _CROSS_FAMILY_PENALTY,
) -> str:
    """Down-weight cross-family notes and apply utility ranking (#2234, #3200)."""
    return rank_by_utility_and_family(
        result_json, consumer_model, cross_family_penalty=penalty
    )


def record_outcome_feedback(
    note_id: str, outcome: str, extra_meta: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Generate structured outcome update payload for a memory note (#3201)."""
    norm_outcome = outcome.strip().lower() if isinstance(outcome, str) else "neutral"
    if norm_outcome not in VALID_OUTCOMES:
        norm_outcome = "neutral"
    payload: Dict[str, Any] = {
        "id": note_id,
        "outcome": norm_outcome,
        "metadata": {
            "outcome": norm_outcome,
            "utility": OUTCOME_UTILITY_WEIGHTS[norm_outcome],
        },
    }
    if isinstance(extra_meta, dict):
        payload["metadata"].update(extra_meta)
    return payload

