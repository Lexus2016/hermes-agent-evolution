"""CompactionRL evaluation signal — record compaction events and summarize outcomes.

Phase 1 (no training): records token/message counts before and after each
compaction step, and whether the post-compaction trajectory succeeded. Over
many cycles, this surfaces which summary patterns correlate with success.

Inspired by CompactionRL (arXiv:2607.05378): the compaction summary is a policy
that should be optimized against downstream outcomes, not a fixed function.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)


def _events_file() -> Path:
    return get_hermes_home() / "logs" / "compaction_events.jsonl"


def record_compaction_event(
    *,
    session_id: str = "",
    messages_before: int = 0,
    messages_after: int = 0,
    tokens_before: int = 0,
    tokens_after: int = 0,
    success: Optional[bool] = None,
) -> None:
    """Log a compaction event with real token/message counts.

    Called from the compaction path in turn_context.py/run_agent.py after a
    compression pass completes. Best-effort; failures log at DEBUG.
    """
    event = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "session_id": (session_id or "")[:200],
        "messages_before": int(messages_before),
        "messages_after": int(messages_after),
        "tokens_before": int(tokens_before),
        "tokens_after": int(tokens_after),
        "success": success,
    }
    try:
        path = _events_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.debug("record_compaction_event failed: %s", e)


def get_eval_summary() -> Dict[str, Any]:
    """Summarize compaction events for the curator/evolution pipeline.

    Returns aggregate stats: total events, avg compression ratio, success rate
    (when success is recorded), and token savings.
    """
    path = _events_file()
    if not path.exists():
        return {"total_events": 0, "avg_ratio": 0.0, "success_rate": None, "total_tokens_saved": 0}
    events: List[Dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line:
                events.append(json.loads(line))
    except Exception:
        return {"total_events": 0, "avg_ratio": 0.0, "success_rate": None, "total_tokens_saved": 0}

    if not events:
        return {"total_events": 0, "avg_ratio": 0.0, "success_rate": None, "total_tokens_saved": 0}

    ratios = []
    tokens_saved = 0
    successes = []
    for e in events:
        tb, ta = e.get("tokens_before", 0), e.get("tokens_after", 0)
        if tb > 0:
            ratios.append(ta / tb)
            tokens_saved += tb - ta
        if e.get("success") is not None:
            successes.append(1 if e["success"] else 0)

    avg_ratio = sum(ratios) / len(ratios) if ratios else 0.0
    success_rate = (sum(successes) / len(successes)) if successes else None
    return {
        "total_events": len(events),
        "avg_ratio": round(avg_ratio, 3),
        "success_rate": round(success_rate, 3) if success_rate is not None else None,
        "total_tokens_saved": tokens_saved,
    }