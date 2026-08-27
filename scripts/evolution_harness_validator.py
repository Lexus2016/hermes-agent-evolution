#!/usr/bin/env python3
"""Score a candidate harness update against the held-out trace batch (#3227).

The Self-Harness loop proposes harness updates (e.g. acting on a mined
weakness cluster). This validator guards against overfitting: a candidate is
SELECTED only if its evidence generalizes across the held-out batch — it is
supported by enough distinct sessions and still clears the recurrence
threshold after holding out the newest sessions — rather than patching a
single trajectory. Deterministic, no LLM, no network.

Depends on ``evolution_trace_holdout.evaluate_holdout`` for the
generalization verdict on the candidate's supporting weaknesses.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, List

from evolution_trace_holdout import (
    DEFAULT_HOLDOUT_RATIO,
    DEFAULT_MIN_COUNT,
    DEFAULT_TOLERANCE,
    evaluate_holdout,
)

DEFAULT_MIN_SESSIONS = 3  # distinct sessions required to call a candidate general


def _cluster_key(record: Dict[str, Any]) -> str:
    """Stable identifier for a weakness cluster (mirrors trace_holdout)."""
    kind = str(record.get("kind", ""))
    tool = str(record.get("tool") or record.get("signature") or "")
    return f"{kind}:{tool}"


def score_candidate(
    candidate: Dict[str, Any],
    holdout_batch: List[Dict[str, Any]],
    *,
    min_sessions: int = DEFAULT_MIN_SESSIONS,
    holdout_ratio: float = DEFAULT_HOLDOUT_RATIO,
    tolerance: float = DEFAULT_TOLERANCE,
    min_count: int = DEFAULT_MIN_COUNT,
) -> Dict[str, Any]:
    """Return a select/reject verdict for one candidate harness update.

    A candidate is SELECTED when BOTH hold:
      1. Its own supporting evidence generalizes: the candidate's weakness
         cluster clears the hold-out recurrence threshold (via
         ``evaluate_holdout``), so it is not a single-trajectory patch.
      2. It is corroborated by the held-out batch: at least ``min_sessions``
         distinct sessions in the batch reference the same cluster key.
    """
    key = str(candidate.get("key") or _cluster_key(candidate))
    occurrences = float(candidate.get("occurrences") or 0)
    sessions = int(candidate.get("sessions") or 0)

    # 1. Generalization of the candidate's own evidence.
    own = evaluate_holdout(
        [
            {
                "kind": key.split(":", 1)[0],
                "tool": key.split(":", 1)[1],
                "occurrences": occurrences,
            }
        ],
        total_sessions=max(sessions, 1),
        holdout_ratio=holdout_ratio,
        tolerance=tolerance,
        min_count=min_count,
    )
    own_generalized = bool(own["clusters"] and own["clusters"][0]["generalized"])

    # 2. Corroboration in the held-out batch.
    batch_sessions = 0
    for rec in holdout_batch:
        rec_key = str(rec.get("key") or _cluster_key(rec))
        if rec_key == key:
            batch_sessions += int(rec.get("sessions") or rec.get("occurrences") or 0)

    selected = own_generalized and batch_sessions >= min_sessions
    return {
        "key": key,
        "occurrences": occurrences,
        "sessions": sessions,
        "own_generalized": own_generalized,
        "batch_sessions": batch_sessions,
        "min_sessions": min_sessions,
        "verdict": "select" if selected else "reject",
    }


def main(argv: List[str]) -> int:
    """CLI: read {candidate, holdout_batch} JSON from stdin, print verdicts."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--min-sessions",
        type=int,
        default=DEFAULT_MIN_SESSIONS,
        help="distinct held-out sessions required to select a candidate",
    )
    args = parser.parse_args(argv)

    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        print(
            f"[evolution-harness-validator] invalid JSON input: {exc}", file=sys.stderr
        )
        return 2

    candidates = payload.get("candidates", []) if isinstance(payload, dict) else []
    holdout_batch = (
        payload.get("holdout_batch", []) if isinstance(payload, dict) else []
    )
    verdicts = [
        score_candidate(c, holdout_batch, min_sessions=args.min_sessions)
        for c in candidates
    ]
    print(json.dumps({"verdicts": verdicts}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
