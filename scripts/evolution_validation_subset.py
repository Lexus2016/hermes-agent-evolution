#!/usr/bin/env python3
"""Cost-reduce the validation stage via task-selection benchmark reduction (#2638).

Task-selection / benchmark-reduction (arXiv:2603.23749): pick a deterministic,
ranking-faithful SUBSET of benchmark tasks so regression checks run on fewer
tasks without distorting the difficulty mix; track validation cost per cycle.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path
from typing import Any, Dict, List, Sequence

__all__ = ["select_subset", "estimate_cost", "track_validation_cost", "main"]

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
    chosen: List[Dict[str, Any]] = []
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


def estimate_cost(
    tasks: Sequence[Dict[str, Any]], cost_per_step: float = 1.0
) -> Dict[str, Any]:
    """Estimate validation cost of a task set (steps * per-step cost)."""
    steps = sum(int(t.get("steps", 1) or 1) for t in tasks)
    return {
        "tasks": len(tasks),
        "steps": steps,
        "estimated_cost": round(steps * cost_per_step, 3),
    }


def track_validation_cost(records: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate per-cycle validation cost from prior validation records."""
    cycles: Dict[str, int] = {}
    total = 0
    for record in records:
        cycle = str(record.get("cycle", "unknown"))
        cost = int(record.get("validation_cost", 0) or 0)
        cycles[cycle] = cycles.get(cycle, 0) + cost
        total += cost
    return {"cycles": cycles, "total_cost": total, "record_count": len(records)}


def main(argv: List[str]) -> int:
    """CLI: reduce a benchmark task list to a ranking-faithful subset."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tasks-json", type=Path, required=True)
    parser.add_argument("--budget-ratio", type=float, default=0.5)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args(argv[1:])
    tasks = json.loads(args.tasks_json.read_text(encoding="utf-8"))
    if not isinstance(tasks, list):
        print(
            "[evolution-validation-subset] tasks JSON must be a list", file=sys.stderr
        )
        return 2
    result = select_subset(tasks, budget_ratio=args.budget_ratio, seed=args.seed)
    result["cost"] = estimate_cost(result["tasks"])
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
