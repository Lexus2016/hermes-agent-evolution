#!/usr/bin/env python3
"""SEA-Eval: evolutionary flywheel convergence as a self-evolution quality metric.

Issue #2249. SEA-Eval's core insight: when an agent successfully distills
historical experience into reusable strategies, execution overhead should
converge monotonically with task frequency — the more the agent encounters a
task type, the less it should cost to solve it. This convergence trajectory
distinguishes genuine evolution (getting faster at repeated task types) from
pseudo-evolution (adding skills but not getting faster).

This module computes the monotonic-convergence criterion from existing
per-cycle telemetry (metrics.jsonl). It is a pure, deterministic, import-safe
module — no creds, no LLM — so it is unit-testable and safe to run as a
``no_agent`` cron job.

The metric answers the question per-cycle issue counts cannot: "is the
evolution pipeline actually making the agent better, or just accumulating?"
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# Allow running as a script from anywhere in the repo.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from evolution_funnel import load_records  # noqa: E402


def _int(rec: Dict[str, Any], key: str) -> int:
    try:
        return int(rec.get(key, 0) or 0)
    except (TypeError, ValueError):
        return 0


def _float(rec: Dict[str, Any], key: str) -> Optional[float]:
    v = rec.get(key)
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _task_type(rec: Dict[str, Any]) -> str:
    """Classify a cycle record into a coarse task type.

    Uses the fields the funnel already records. Falls back to 'general' when
    no discriminating signal is present. This is intentionally coarse — the
    goal is a per-task-type cost trend, not a precise taxonomy.
    """
    if _int(rec, "research_proposals") > 0:
        return "research"
    if _int(rec, "introspection_patterns") > 0:
        return "introspection"
    if _int(rec, "issues_created") > 0:
        return "issue-creation"
    return "general"


def _execution_cost(rec: Dict[str, Any]) -> Optional[float]:
    """Per-cycle execution cost proxy.

    Prefers an explicit ``execution_cost`` field if present (tokens, tool
    calls, or time). Otherwise derives a proxy from the funnel's own output
    volume: total work items produced that cycle. A lower cost for the same
    task type over time is the convergence signal.
    """
    explicit = _float(rec, "execution_cost")
    if explicit is not None:
        return explicit
    # Proxy: total pipeline output volume for the cycle. More output at
    # similar effort is cheaper per unit of work.
    return float(
        _int(rec, "issues_created")
        + _int(rec, "selected")
        + _int(rec, "merged")
        + _int(rec, "research_proposals")
        + _int(rec, "introspection_patterns")
    )


def _is_converging(costs: List[float]) -> bool:
    """Monotonic-convergence check for a task type's cost series.

    A task type is converging only if its cost is STRICTLY decreasing over
    time (each successive observation is < the previous one, within a small
    tolerance to avoid flagging noise). A flat series (equal cost) is NOT
    converging — the agent is accumulating, not getting faster. Requires at
    least 2 observations.
    """
    if len(costs) < 2:
        return False
    tol = 1e-6
    for a, b in zip(costs, costs[1:]):
        if b >= a - tol:
            return False
    return True


def _trend(costs: List[float]) -> str:
    """Classify a cost series as converging / flat / diverging / n/a."""
    if len(costs) < 2:
        return "n/a"
    if _is_converging(costs):
        return "converging"
    # Diverging if the last observation is meaningfully above the first.
    if costs[-1] > costs[0] * 1.15:
        return "diverging"
    return "flat"


def compute_convergence(
    records: List[Dict[str, Any]],
    min_occurrences: int = 3,
) -> Dict[str, Any]:
    """Compute the SEA-Eval convergence criterion across cycle records.

    Args:
        records: list of per-cycle funnel records (from metrics.jsonl).
        min_occurrences: a task type must appear at least this many times to
            be judged for convergence (avoids noise on rare types).

    Returns:
        A dict with per-task-type cost series, trend classification, and a
        list of non-converging task types flagged for skill-capture/retrieval
        review.
    """
    # Group cost observations by task type, in chronological order.
    by_type: Dict[str, List[float]] = {}
    for rec in records:
        tt = _task_type(rec)
        cost = _execution_cost(rec)
        if cost is None:
            continue
        by_type.setdefault(tt, []).append(cost)

    per_type: Dict[str, Dict[str, Any]] = {}
    non_converging: List[str] = []
    for tt, costs in sorted(by_type.items()):
        # A task type with too few occurrences is not judged at all — its
        # trend is "n/a" (insufficient signal), and it is never flagged.
        judged = len(costs) >= min_occurrences
        entry = {
            "task_type": tt,
            "occurrences": len(costs),
            "cost_series": costs,
            "trend": _trend(costs) if judged else "n/a",
        }
        per_type[tt] = entry
        if judged and entry["trend"] != "converging":
            non_converging.append(tt)

    return {
        "criterion": "monotonic-convergence",
        "min_occurrences": min_occurrences,
        "per_task_type": per_type,
        "non_converging_task_types": non_converging,
        "genuine_evolution": len(non_converging) == 0,
    }


def _load_records(evolution_dir: Path) -> List[Dict[str, Any]]:
    """Load funnel records from the evolution dir's metrics.jsonl."""
    metrics_path = evolution_dir / "metrics.jsonl"
    if not metrics_path.exists():
        return []
    return load_records(metrics_path)


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="SEA-Eval: evolutionary flywheel convergence metric."
    )
    parser.add_argument(
        "--evolution-dir",
        default=os.environ.get(
            "EVOLUTION_PROFILE_DIR",
            str(Path.home() / ".hermes" / "evolution"),
        ),
        help="Path to the evolution directory holding metrics.jsonl.",
    )
    parser.add_argument(
        "--min-occurrences",
        type=int,
        default=3,
        help="Min occurrences for a task type to be judged for convergence.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of a human summary.",
    )
    args = parser.parse_args(argv)

    records = _load_records(Path(args.evolution_dir))
    result = compute_convergence(records, min_occurrences=args.min_occurrences)

    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    print(f"SEA-Eval convergence criterion (min_occurrences={args.min_occurrences}):")
    print(f"  genuine_evolution: {result['genuine_evolution']}")
    for tt, entry in result["per_task_type"].items():
        print(
            f"  {tt}: occurrences={entry['occurrences']} "
            f"trend={entry['trend']} cost_series={entry['cost_series']}"
        )
    if result["non_converging_task_types"]:
        print(
            "  FLAG: non-converging task types -> "
            + ", ".join(result["non_converging_task_types"])
            + " (review skill capture/retrieval)"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
