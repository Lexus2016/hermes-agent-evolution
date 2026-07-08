#!/usr/bin/env python3
"""Parallel draft mode and cost-aware routing for multi-agent coordination (#798).

Complements the existing evolution_orchestrator.py (fan-out by DIFFERENT angles)
with parallel drafts of the SAME task plus model cost-tier routing.

  * ``build_draft_tasks`` — N identical delegate_task payloads for parallel
    drafting (multiple agents attempt the same task).
  * ``select_best_draft`` — pick the best draft from parallel results, using
    explicit scores if provided or a heuristic otherwise.
  * ``route_cost_tier`` — map a 0-1 complexity score to a recommended model
    cost tier (frontier / standard / economy).

Note: delegate_task does not support per-call model selection — children
inherit the parent model. Cost-aware routing is therefore a CONFIGURATION
RECOMMENDATION layer: it maps complexity to a tier which the caller uses
when setting delegation.model in config.yaml.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

COST_TIERS: tuple[str, ...] = ("frontier", "standard", "economy")
_OK_STATUSES = frozenset({"completed", "success", "ok"})
_COMPLEXITY_LOW = 0.3
_COMPLEXITY_HIGH = 0.7


def route_cost_tier(complexity: float) -> str:
    """Map a complexity score (0.0–1.0) to a recommended model cost tier.

    < 0.3 → economy, < 0.7 → standard, else frontier.
    """
    complexity = max(0.0, min(1.0, complexity))
    if complexity < _COMPLEXITY_LOW:
        return "economy"
    if complexity < _COMPLEXITY_HIGH:
        return "standard"
    return "frontier"


def build_draft_tasks(
    goal: str,
    n_drafters: int = 2,
    *,
    context: str = "",
    toolsets: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Build N identical leaf-worker tasks for parallel draft mode.

    Each drafter gets the same goal — the orchestrator selects the best result
    after collection. Returns a list of delegate_task-compatible dicts.
    """
    if n_drafters < 1:
        n_drafters = 1
    task = {
        "goal": goal,
        "context": context or (goal[:200] if isinstance(goal, str) else ""),
        "role": "leaf",
        "toolsets": list(toolsets) if toolsets else ["file"],
    }
    return [task.copy() for _ in range(n_drafters)]


def select_best_draft(
    results: List[Dict[str, Any]],
    *,
    scores: Optional[List[float]] = None,
) -> Dict[str, Any]:
    """Select the best draft from parallel delegate_task results.

    If *scores* are provided (same length as *results*), picks the highest.
    Otherwise uses a heuristic: prefer completed/success status, then longest
    summary (proxy for thoroughness). Returns a dict with selected_index,
    result, and reason.
    """
    if not results:
        return {"selected_index": -1, "result": None, "reason": "no drafts"}

    if scores and len(scores) == len(results):
        best_idx = max(range(len(scores)), key=lambda i: scores[i])
        return {
            "selected_index": best_idx,
            "result": results[best_idx],
            "reason": "highest_score",
        }

    ok = [(i, r) for i, r in enumerate(results) if r.get("status", "") in _OK_STATUSES]
    pool = ok if ok else list(enumerate(results))
    best_idx = max(pool, key=lambda pair: len(str(pair[1].get("summary", ""))))[0]
    return {
        "selected_index": best_idx,
        "result": results[best_idx],
        "reason": "heuristic",
    }
