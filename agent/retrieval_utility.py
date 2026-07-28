"""Retrieval-utility logging + history-based deletion (issue #1480, child of #1270).

Tracks every memory/skill retrieval and its downstream outcome in a sidecar
JSON file so we can measure whether a record actually helped the agent.

Two-step pipeline:
  1. **Retrieval-utility log** — each time ``MemoryManager.prefetch_all``
     returns context, the caller logs a retrieval entry with (record_id,
     timestamp). When ``sync_all`` runs at turn end, the
     caller records the downstream outcome (derived from friction signals:
     retries, task_failures, human_corrections) against the retrievals that
     were active for that turn.
  2. **History-based deletion** — ``delete_low_utility_records`` removes
     records retrieved ≥ *n* times whose average downstream utility falls
     below a configurable floor. The ACL 2026 memory-management paper proves
     selective addition + history-based deletion beats add-all by 22-25
     points.

Design notes (mirroring ``tools/skill_usage.py``):
  - Sidecar, not frontmatter — operational telemetry stays out of
    user-authored memory/skill content.
  - Atomic writes via tempfile + os.replace.
  - All counter bumps are best-effort: failures log at DEBUG and return
    silently. A broken sidecar never breaks the underlying memory system.
  - Profile-aware via ``get_hermes_home()``.
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

# Default thresholds for history-based deletion.
_DEFAULT_MIN_RETRIEVALS = 3
_DEFAULT_UTILITY_FLOOR = 0.5
# Cap log size to prevent unbounded growth — old entries are pruned
# on write when the list exceeds this count.
_MAX_LOG_ENTRIES = 5000

# fcntl is Unix-only; on Windows use msvcrt for file locking.
msvcrt = None
try:
    import fcntl
except ImportError:  # pragma: no cover - platform-specific fallback
    fcntl = None
    try:
        import msvcrt
    except ImportError:
        pass


def _utility_file() -> Path:
    """Return the sidecar path for the retrieval-utility log."""
    return get_hermes_home() / "memory" / ".retrieval_utility.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


import contextlib


@contextlib.contextmanager
def _lock():
    """Cross-process file lock for the sidecar (same pattern as skill_usage)."""
    lock_path = _utility_file().with_suffix(".json.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    if fcntl is None and msvcrt is None:
        yield  # no locking available
        return

    if msvcrt and (not lock_path.exists() or lock_path.stat().st_size == 0):
        lock_path.write_text(" ", encoding="utf-8")

    fd = open(lock_path, "r+" if msvcrt else "a+", encoding="utf-8")
    try:
        if fcntl:
            fcntl.flock(fd, fcntl.LOCK_EX)
        else:  # msvcrt
            fd.seek(0)
            msvcrt.locking(fd.fileno(), msvcrt.LK_LOCK, 1)  # type: ignore[union-attr]
        yield
    finally:
        if fcntl:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except (OSError, IOError):
                pass
        elif msvcrt:
            try:
                fd.seek(0)
                msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[union-attr]
            except (OSError, IOError):
                pass
        fd.close()


def load_log() -> Dict[str, Any]:
    """Load the retrieval-utility sidecar.

    Returns ``{"retrievals": [...], "outcomes": {...}}``. On missing/corrupt
    file, returns an empty structure.
    """
    path = _utility_file()
    if not path.exists():
        return {"retrievals": [], "outcomes": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.debug("retrieval_utility: could not read sidecar: %s", e)
        return {"retrievals": [], "outcomes": {}}
    if not isinstance(data, dict):
        return {"retrievals": [], "outcomes": {}}
    data.setdefault("retrievals", [])
    data.setdefault("outcomes", {})
    return data


def save_log(data: Dict[str, Any]) -> None:
    """Atomically write the retrieval-utility sidecar."""
    path = _utility_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent), prefix=".retrieval_utility_", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def record_retrieval(
    record_id: str,
    retrieval_context: str = "",
    *,
    session_id: str = "",
) -> None:
    """Log that *record_id* was retrieved.

    ``retrieval_context`` is ACCEPTED but NOT STORED. Utility is computed from
    outcomes alone (see :func:`compute_utility`), so the query text was write-
    only — never read back, and a fragment of the user's prompt on disk for no
    downstream purpose. The parameter stays in the signature so callers need no
    change and a future consumer can opt into storing a redacted form
    deliberately rather than by default.

    Best-effort: failures log at DEBUG and return silently.
    """
    if not record_id:
        return
    try:
        with _lock():
            data = load_log()
            data.setdefault("retrievals", []).append({
                "record_id": record_id,
                "session_id": session_id,
                "timestamp": _now_iso(),
                "outcome": None,  # filled by record_outcome
            })
            # Prune oldest entries if log exceeds the cap.
            if len(data["retrievals"]) > _MAX_LOG_ENTRIES:
                data["retrievals"] = data["retrievals"][-_MAX_LOG_ENTRIES:]
            save_log(data)
    except Exception as e:
        logger.debug("retrieval_utility.record_retrieval(%s) failed: %s", record_id, e)


def record_outcome(
    record_id: str,
    *,
    outcome: str = "unknown",
    friction_signals: Optional[Dict[str, int]] = None,
) -> None:
    """Record the downstream outcome for a retrieval of *record_id*.

    Called from ``sync_all`` after the turn completes. ``outcome`` is a
    coarse label: ``"helpful"``, ``"neutral"``, or ``"harmful"``. Friction
    signals (retries, task_failures, human_corrections) drive the label:

    - No friction signals → ``"helpful"`` (the retrieval contributed without
      issues).
    - task_failures or human_corrections present → ``"harmful"`` (the
      retrieval may have contributed to a bad outcome).
    - retries only → ``"neutral"`` (recoverable friction).

    Best-effort: failures log at DEBUG and return silently.
    """
    if not record_id:
        return
    try:
        with _lock():
            data = load_log()
            retrievals = data.get("retrievals", [])
            # Find the most recent retrieval with no outcome yet.
            for entry in reversed(retrievals):
                if entry.get("record_id") == record_id and entry.get("outcome") is None:
                    entry["outcome"] = outcome
                    entry["friction_signals"] = dict(friction_signals or {})
                    break
            else:
                # No matching pending retrieval — stale outcome, skip.
                return
            save_log(data)
    except Exception as e:
        logger.debug("retrieval_utility.record_outcome(%s) failed: %s", record_id, e)


def derive_outcome(friction_signals: Dict[str, int]) -> str:
    """Derive a coarse outcome label from friction signals.

    >>> derive_outcome({})
    'helpful'
    >>> derive_outcome({"retries": 2})
    'neutral'
    >>> derive_outcome({"task_failures": 1})
    'harmful'
    >>> derive_outcome({"human_corrections": 1})
    'harmful'
    """
    if not friction_signals:
        return "helpful"
    if friction_signals.get("task_failures") or friction_signals.get(
        "human_corrections"
    ):
        return "harmful"
    if friction_signals.get("retries"):
        return "neutral"
    return "helpful"


def compute_utility(record_id: str) -> Optional[Dict[str, Any]]:
    """Aggregate retrieval-utility stats for *record_id*.

    Returns ``None`` if no retrievals exist. Otherwise returns::

        {
            "record_id": <id>,
            "retrieval_count": <int>,
            "avg_utility": <float>,  # 1.0 helpful, 0.5 neutral, 0.0 harmful
            "outcomes": {"helpful": N, "neutral": N, "harmful": N},
        }
    """
    data = load_log()
    retrievals = [
        r for r in data.get("retrievals", []) if r.get("record_id") == record_id
    ]
    if not retrievals:
        return None
    outcome_scores = {"helpful": 1.0, "neutral": 0.5, "harmful": 0.0, "unknown": 0.5}
    counts: Dict[str, int] = {}
    total = 0.0
    matched = 0
    for r in retrievals:
        outcome = r.get("outcome") or "unknown"
        counts[outcome] = counts.get(outcome, 0) + 1
        if outcome != "unknown":
            total += outcome_scores.get(outcome, 0.5)
            matched += 1
    avg = total / matched if matched > 0 else 0.5
    return {
        "record_id": record_id,
        "retrieval_count": len(retrievals),
        "avg_utility": round(avg, 4),
        "outcomes": counts,
    }


def delete_low_utility_records(
    min_retrievals: int = _DEFAULT_MIN_RETRIEVALS,
    utility_floor: float = _DEFAULT_UTILITY_FLOOR,
) -> List[str]:
    """Identify records eligible for history-based deletion.

    A record is eligible if it was retrieved ≥ *min_retrievals* times and its
    average downstream utility is below *utility_floor*.

    Returns the list of eligible record IDs (does NOT mutate the log).
    The caller (memory manager / CLI) decides whether to actually remove
    the records from the memory store.
    """
    data = load_log()
    # Group by record_id.
    by_record: Dict[str, List[Dict[str, Any]]] = {}
    for r in data.get("retrievals", []):
        rid = r.get("record_id")
        if rid:
            by_record.setdefault(rid, []).append(r)

    eligible: List[str] = []
    outcome_scores = {"helpful": 1.0, "neutral": 0.5, "harmful": 0.0, "unknown": 0.5}
    for rid, retrievals in by_record.items():
        if len(retrievals) < min_retrievals:
            continue
        matched = 0
        total = 0.0
        for r in retrievals:
            outcome = r.get("outcome") or "unknown"
            if outcome != "unknown":
                total += outcome_scores.get(outcome, 0.5)
                matched += 1
        avg = total / matched if matched > 0 else 0.5
        if avg < utility_floor:
            eligible.append(rid)

    return sorted(eligible)


def clear_log() -> None:
    """Clear all retrieval-utility entries (used by tests / manual reset)."""
    try:
        with _lock():
            save_log({"retrievals": [], "outcomes": {}})
    except Exception as e:
        logger.debug("retrieval_utility.clear_log() failed: %s", e)
