"""Cost-reduce validation via task-selection benchmark reduction (#2638).

Picks a deterministic, difficulty-mix-preserving SUBSET of validation tasks so
regression checks run on fewer tasks; tracks validation cost per cycle.
"""

from __future__ import annotations

import random
from typing import Any, Dict, Sequence

__all__ = ["DIFFICULTY_BANDS", "select_subset", "track_validation_cost"]

DIFFICULTY_BANDS = ("easy", "medium", "hard")


def _band(task: Dict[str, Any]) -> str:
    difficulty = str(task.get("difficulty", "medium")).lower()
    return difficulty if difficulty in DIFFICULTY_BANDS else "medium"


def select_subset(
    tasks: Sequence[Dict[str, Any]], budget_ratio: float = 0.5, seed: int = 7
) -> Dict[str, Any]:
    """Deterministic stratified subset that preserves the difficulty mix."""
    rng = random.Random(seed)
    tasks = list(tasks)
    total = len(tasks)
    if not 0 < budget_ratio <= 1:
        raise ValueError("budget_ratio must be in (0, 1]")
    budget = max(1, round(total * budget_ratio))
    chosen = []
    for band in DIFFICULTY_BANDS:
        items = [t for t in tasks if _band(t) == band]
        if items:
            take = max(1, round(len(items) * budget_ratio))
            chosen.extend(rng.sample(items, min(take, len(items))))
    if len(chosen) > budget:
        chosen = rng.sample(chosen, budget)
    return {
        "tasks": chosen,
        "stats": {
            "total": total,
            "subset": len(chosen),
            "budget_ratio": budget_ratio,
            "savings_ratio": round(1 - (len(chosen) / total if total else 0.0), 3),
        },
    }


def track_validation_cost(records: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate per-cycle validation cost from validation cost records."""
    cycles: Dict[str, int] = {}
    total = 0
    for record in records:
        cycle = str(record.get("cycle", "unknown"))
        cost = int(record.get("validation_cost", 0) or 0)
        cycles[cycle] = cycles.get(cycle, 0) + cost
        total += cost
    return {"cycles": cycles, "total_cost": total, "record_count": len(records)}
