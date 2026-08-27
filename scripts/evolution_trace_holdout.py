#!/usr/bin/env python3
"""Deterministic trace hold-out evaluator for the Self-Harness loop (#3226).

``evolution_trace_miner`` calls ``evaluate_holdout(...)`` after mining, so the
weekly cron gates weaknesses automatically. A cluster is **generalized** only if
it would still clear the recurrence threshold after ignoring the newest
``holdout_ratio`` of sessions (proxied as ``occurrences * (1 - holdout_ratio) >=
min_count * (1 - tolerance)``); a recent spike alone must not drive a code
change. Deterministic, no LLM, no network.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Dict, List

DEFAULT_HOLDOUT_RATIO = 0.20
DEFAULT_TOLERANCE = 0.20
DEFAULT_MIN_COUNT = 5  # must match evolution_trace_miner.DEFAULT_MIN_COUNT


def _cluster_key(record: Dict[str, Any]) -> str:
    """Stable identifier for a weakness cluster."""
    kind = str(record.get("kind", ""))
    tool = str(record.get("tool") or record.get("signature") or "")
    return f"{kind}:{tool}"


def evaluate_holdout(
    weaknesses: List[Dict[str, Any]],
    total_sessions: int,
    *,
    holdout_ratio: float = DEFAULT_HOLDOUT_RATIO,
    tolerance: float = DEFAULT_TOLERANCE,
    min_count: int = DEFAULT_MIN_COUNT,
) -> Dict[str, Any]:
    """Return a deterministic generalization report for the mined weaknesses.

    A cluster is generalized when the evidence that would remain after holding
    out the newest ``holdout_ratio`` of sessions still clears the recurrence
    threshold. ``total_sessions`` is report context (evidence breadth), not part
    of the verdict. Clusters flagged ``generalized == False`` need more evidence
    across time before the agent proposes a code change for them.
    """
    results: List[Dict[str, Any]] = []
    generalized_count = 0
    threshold = min_count * (1.0 - tolerance)
    for record in weaknesses:
        occurrences = float(record.get("occurrences") or record.get("severity") or 0)
        train_occurrences = occurrences * (1.0 - holdout_ratio)
        generalized = train_occurrences >= threshold
        if generalized:
            generalized_count += 1
        results.append({
            "key": _cluster_key(record),
            "occurrences": occurrences,
            "train_occurrences": round(train_occurrences, 2),
            "threshold": round(threshold, 2),
            "generalized": generalized,
        })

    return {
        "total_clusters": len(results),
        "generalized": generalized_count,
        "not_generalized": len(results) - generalized_count,
        "holdout_ratio": holdout_ratio,
        "tolerance": tolerance,
        "min_count": min_count,
        "total_sessions": total_sessions,
        "clusters": results,
    }


def main(argv: List[str]) -> int:
    """CLI: read weakness JSON from stdin, write holdout report to stdout."""
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        print(f"[evolution-trace-holdout] invalid JSON input: {exc}", file=sys.stderr)
        return 2

    weaknesses = payload.get("weaknesses", []) if isinstance(payload, dict) else []
    total_sessions = (
        int(payload.get("sessions_scanned", 0)) if isinstance(payload, dict) else 0
    )
    report = evaluate_holdout(weaknesses, total_sessions)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
