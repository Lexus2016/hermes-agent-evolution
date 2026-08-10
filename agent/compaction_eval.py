"""Post-compaction trajectory evaluation signal (#2185, Phase 1).

Records whether the post-compaction trajectory succeeds so the pipeline
can correlate summary quality with downstream task outcomes.

This is the Phase 1 evaluation signal from CompactionRL (arXiv:2607.05378).
Phase 2 (mutable compaction skill + evolution-pipeline training) is deferred.

The module records two events to a sidecar JSON (~/.hermes/compaction_eval.json):
1. Compaction event: timestamp, token reduction, message count change
2. Turn outcome: success/failure/interrupted after compaction

Design:
  - Sidecar JSON, not inline state — keeps eval data out of conversation state.
  - Best-effort: failures log at DEBUG and return silently.
  - Each record is keyed by turn_id so the compaction event and its outcome
    can be correlated.
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

_MAX_RECORDS = 200  # cap to prevent unbounded growth


def _eval_file() -> Path:
    return get_hermes_home() / "compaction_eval.json"


def _load() -> List[Dict[str, Any]]:
    path = _eval_file()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _save(records: List[Dict[str, Any]]) -> None:
    path = _eval_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(records[-_MAX_RECORDS:], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp, path)


def record_compaction_event(
    turn_id: str,
    session_id: str = "",
    messages_before: int = 0,
    messages_after: int = 0,
    tokens_before: int = 0,
    tokens_after: int = 0,
) -> None:
    """Record that a compaction event occurred during a turn (#2185).

    Called after compaction completes. The turn_id links this event to the
    later outcome recorded by record_post_compaction_outcome().
    Best-effort; failures log at DEBUG and return silently.
    """
    if not turn_id:
        return
    try:
        records = _load()
        records.append({
            "turn_id": turn_id[:200],
            "session_id": (session_id or "")[:200],
            "event": "compaction",
            "messages_before": messages_before,
            "messages_after": messages_after,
            "tokens_before": tokens_before,
            "tokens_after": tokens_after,
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })
        _save(records)
    except Exception as e:
        logger.debug("record_compaction_event failed: %s", e, exc_info=True)


def record_post_compaction_outcome(
    turn_id: str,
    success: bool,
    interrupted: bool = False,
    failed: bool = False,
) -> None:
    """Record the outcome of a turn that included compaction (#2185).

    Called from finalize_turn when the turn exits. Only records if the
    turn had a compaction event (matched by turn_id).
    Best-effort; failures log at DEBUG and return silently.
    """
    if not turn_id:
        return
    try:
        records = _load()
        # Find the compaction event for this turn_id.
        has_compaction = any(
            r.get("turn_id") == turn_id and r.get("event") == "compaction"
            for r in records
        )
        if not has_compaction:
            return
        records.append({
            "turn_id": turn_id[:200],
            "event": "outcome",
            "success": success,
            "interrupted": interrupted,
            "failed": failed,
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })
        _save(records)
    except Exception as e:
        logger.debug("record_post_compaction_outcome failed: %s", e, exc_info=True)


def get_eval_summary() -> Dict[str, Any]:
    """Return a summary of compaction evaluation data.

    Used by the pipeline / curator to surface which summary patterns
    correlate with success.
    """
    records = _load()
    compaction_events = [r for r in records if r.get("event") == "compaction"]
    outcomes = [r for r in records if r.get("event") == "outcome"]
    # Match outcomes to compaction events by turn_id.
    outcome_by_turn = {r["turn_id"]: r for r in outcomes}
    matched = 0
    successes = 0
    failures = 0
    interrupted = 0
    for ev in compaction_events:
        outcome = outcome_by_turn.get(ev["turn_id"])
        if outcome:
            matched += 1
            if outcome.get("success"):
                successes += 1
            elif outcome.get("failed"):
                failures += 1
            elif outcome.get("interrupted"):
                interrupted += 1
    return {
        "total_compactions": len(compaction_events),
        "total_outcomes": len(outcomes),
        "matched": matched,
        "successes": successes,
        "failures": failures,
        "interrupted": interrupted,
        "success_rate": successes / matched if matched else None,
    }
