"""Per-skill provenance score record (#2190, Slice B of #2181).

Extends the existing ``skill_provenance.py`` (write-origin ContextVar)
with a **persistent per-skill provenance record** stored in the usage
sidecar:

    source_run_id:     identifier of the run that created the skill
    created_at:        ISO timestamp of skill creation
    invocation_count:  incremented on each skill execution
    recent_failure_rate: rolling failure rate [0.0, 1.0]

This gives the pipeline (and the validation gate from Slice A) the data
to reason about each skill's history — identifying skills that were
invoked many times but have a high failure rate (candidates for review
or auto-revert in Slice C).

arXiv:2608.05810: "Per-commit verification is a structural requirement"
— provenance metadata is what makes regression correlation possible.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Sliding window for failure-rate calculation
_FAILURE_WINDOW = 20


def init_provenance_record(
    skill_name: str, source_run_id: Optional[str] = None
) -> None:
    """Initialize the provenance record for a newly admitted skill.

    Called at skill creation time (after all gates pass). Sets source_run_id
    and created_at, zeroes the invocation/failure counters.

    Args:
        skill_name: the skill being admitted
        source_run_id: identifier of the run that created the skill
                       (auto-generated UUID if not provided)
    """
    run_id = source_run_id or str(uuid.uuid4())[:12]
    try:
        from tools.skill_usage import _mutate

        def _apply(rec: Dict[str, Any]) -> None:
            rec["source_run_id"] = run_id
            rec["created_at"] = datetime.now(timezone.utc).isoformat()
            rec["invocation_count"] = 0
            rec["recent_failure_rate"] = 0.0
            rec["failure_history"] = []

        _mutate(skill_name, _apply)
    except Exception as e:
        logger.debug("init_provenance_record(%s): %s", skill_name, e)


def record_invocation(skill_name: str, failed: bool = False) -> None:
    """Increment invocation_count and update the rolling failure rate.

    Called on each skill execution. Maintains a sliding-window failure
    history to compute ``recent_failure_rate``.

    Args:
        skill_name: the skill that was invoked
        failed: whether the invocation resulted in a failure
    """
    try:
        from tools.skill_usage import _mutate

        def _apply(rec: Dict[str, Any]) -> None:
            rec["invocation_count"] = int(rec.get("invocation_count") or 0) + 1
            history = rec.get("failure_history")
            if not isinstance(history, list):
                history = []
            history.append(1 if failed else 0)
            # Trim to sliding window
            if len(history) > _FAILURE_WINDOW:
                history = history[-_FAILURE_WINDOW:]
            rec["failure_history"] = history
            rec["recent_failure_rate"] = round(sum(history) / len(history), 4)

        _mutate(skill_name, _apply)
    except Exception as e:
        logger.debug("record_invocation(%s): %s", skill_name, e)


def get_provenance_record(skill_name: str) -> Dict[str, Any]:
    """Return the provenance record for a skill.

    Returns a dict with the 4 required fields (source_run_id, created_at,
    invocation_count, recent_failure_rate) plus the raw failure_history.
    Missing fields default to zero/empty values.
    """
    defaults = {
        "source_run_id": None,
        "created_at": None,
        "invocation_count": 0,
        "recent_failure_rate": 0.0,
        "failure_history": [],
    }
    try:
        from tools.skill_usage import get_record

        rec = get_record(skill_name)
        for key, default in defaults.items():
            if key not in rec:
                rec[key] = default
        return rec
    except Exception:
        return dict(defaults)
