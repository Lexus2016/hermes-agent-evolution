#!/usr/bin/env python3
"""Retrieval-utility logging + history-based deletion (issue #1480, child of #1270).

The ACL-2026 memory-management paper proves an **add-all** memory policy
self-degrades: records retrieved often but with poor downstream outcomes get
imitated, amplifying errors. The fix is to track **retrieval utility** — did
the retrieval of a record correlate with a successful task outcome? — and
delete records with persistently low utility.

This module provides the two halves of that mechanism for the pipeline's own
artifacts (heuristics, skills, memories):

1. **Retrieval-utility log** — append-only JSONL at
   ``<EVOLUTION_PROFILE_DIR>/retrieval-utility.jsonl``. Each line records a
   retrieval event: *which* record was retrieved, *when*, for *which* task
   (paired by ``task_key``), and the *downstream outcome* (from trajectory
   ``completed``). Outcome is recorded at retrieval time when known, or later
   via :func:`update_outcome` when the trajectory completes after retrieval.

2. **History-based deletion** — analyze the accumulated log and identify
   records retrieved ≥ ``min_retrievals`` times whose average downstream
   utility falls below ``utility_threshold``. Returns a deletion plan the
   caller (funnel / maintenance cron) can act on.

Design follows the sibling ``evolution_*.py`` helpers: pure functions, thin
CLI, import-safe, deterministic, no LLM, fail-open (never raises on I/O).
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

__all__ = [
    "UtilityRecord",
    "log_retrieval",
    "update_outcome",
    "load_utility_log",
    "DeletionCandidate",
    "compute_deletion_candidates",
    "apply_deletions",
    "main",
]

#: Minimum retrievals before a record is eligible for deletion evaluation.
#: Below this the sample is too small to judge.
DEFAULT_MIN_RETRIEVALS = 3

#: Records with average utility below this are deletion candidates.
#: 0.5 means "succeeded less than half the time it was retrieved".
DEFAULT_UTILITY_THRESHOLD = 0.5


def _default_evolution_dir() -> Path:
    """Resolve the evolution profile directory from env, matching siblings."""
    env = os.environ.get("EVOLUTION_PROFILE_DIR", "").strip()
    if env:
        return Path(env)
    hh = os.environ.get("HERMES_HOME", "").strip()
    return Path(hh) / "evolution" if hh else Path.home() / ".hermes" / "evolution"


def _utility_log_path(evolution_dir: Optional[Path] = None) -> Path:
    """Path to the append-only retrieval-utility JSONL."""
    return (evolution_dir or _default_evolution_dir()) / "retrieval-utility.jsonl"


@dataclass
class UtilityRecord:
    """One retrieval event in the utility log.

    ``outcome`` is tri-state: ``True`` = task succeeded, ``False`` = task
    failed, ``None`` = outcome not yet known (trajectory pending or pre-#1363).
    Records with ``None`` outcome are excluded from deletion scoring — we do
    not guess success or failure.
    """

    record_id: str
    record_type: str  # "heuristic" | "skill" | "memory" | ...
    retrieved_at: str = ""
    task_key: str = ""
    outcome: Optional[bool] = None
    task_type: str = ""

    def __post_init__(self) -> None:
        if not self.retrieved_at:
            self.retrieved_at = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "record_id": self.record_id,
            "record_type": self.record_type,
            "retrieved_at": self.retrieved_at,
        }
        if self.task_key:
            d["task_key"] = self.task_key
        if self.outcome is not None:
            d["outcome"] = self.outcome
        if self.task_type:
            d["task_type"] = self.task_type
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "UtilityRecord":
        outcome = d.get("outcome")
        # Normalise outcome: explicit bool or None. Never coerce.
        if outcome is not None and not isinstance(outcome, bool):
            outcome = None
        return cls(
            record_id=str(d.get("record_id", "")),
            record_type=str(d.get("record_type", "")),
            retrieved_at=str(d.get("retrieved_at", "")),
            task_key=str(d.get("task_key", "")),
            outcome=outcome,
            task_type=str(d.get("task_type", "")),
        )


def log_retrieval(
    record_id: str,
    record_type: str = "heuristic",
    *,
    task_key: str = "",
    outcome: Optional[bool] = None,
    task_type: str = "",
    evolution_dir: Optional[Path] = None,
) -> Optional[Path]:
    """Append a retrieval event to the utility log. Returns path or None.

    Never raises — runs alongside retrieval paths that must not break.
    ``record_id`` should be a stable identifier (heuristic text hash, skill
    name, memory key) so multiple retrievals of the same record accumulate.
    """
    if not record_id:
        return None
    try:
        path = _utility_log_path(evolution_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        rec = UtilityRecord(
            record_id=record_id,
            record_type=record_type,
            task_key=task_key,
            outcome=outcome,
            task_type=task_type,
        )
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec.to_dict(), sort_keys=True) + "\n")
        return path
    except Exception:
        return None


def update_outcome(
    task_key: str,
    outcome: bool,
    *,
    evolution_dir: Optional[Path] = None,
) -> int:
    """Backfill ``outcome`` on all log entries matching ``task_key``.

    Called when a trajectory completes *after* its retrievals were logged
    (the common case: retrieval happens at task start, outcome at task end).
    Returns the number of entries updated. Never raises.
    """
    if not task_key:
        return 0
    try:
        path = _utility_log_path(evolution_dir)
        if not path.exists():
            return 0
        lines = path.read_text(encoding="utf-8").splitlines()
        updated = 0
        out_lines: List[str] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except (ValueError, TypeError):
                out_lines.append(line)
                continue
            if (
                isinstance(d, dict)
                and d.get("task_key") == task_key
                and d.get("outcome") is None
            ):
                d["outcome"] = outcome
                updated += 1
            out_lines.append(json.dumps(d, sort_keys=True))
        if updated:
            path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
        return updated
    except Exception:
        return 0


def load_utility_log(
    evolution_dir: Optional[Path] = None,
) -> List[UtilityRecord]:
    """Read the full utility log as a list of :class:`UtilityRecord`.

    Malformed lines are skipped, never raised. Returns [] if absent.
    """
    path = _utility_log_path(evolution_dir)
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    records: List[UtilityRecord] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(d, dict) and d.get("record_id"):
            records.append(UtilityRecord.from_dict(d))
    return records


@dataclass
class DeletionCandidate:
    """A record recommended for deletion based on low accumulated utility."""

    record_id: str
    record_type: str
    retrieval_count: int
    scored_count: int  # retrievals with a known outcome
    avg_utility: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "record_id": self.record_id,
            "record_type": self.record_type,
            "retrieval_count": self.retrieval_count,
            "scored_count": self.scored_count,
            "avg_utility": round(self.avg_utility, 4),
        }


def compute_deletion_candidates(
    records: Sequence[UtilityRecord],
    *,
    min_retrievals: int = DEFAULT_MIN_RETRIEVALS,
    utility_threshold: float = DEFAULT_UTILITY_THRESHOLD,
) -> List[DeletionCandidate]:
    """Identify records with persistently low downstream utility.

    A record becomes a deletion candidate when:
    - It was retrieved at least ``min_retrievals`` times (small samples are
      noisy).
    - At least one retrieval has a known outcome (``None`` outcomes are
      excluded — we do not guess).
    - The average outcome (success=1, failure=0) falls below
      ``utility_threshold``.

    Sorted worst-first (lowest utility, then most retrievals) so the caller
    can cap the number acted on.
    """
    # Group scored retrievals by (record_id, record_type).
    stats: Dict[tuple[str, str], Dict[str, Any]] = {}
    total_retrievals: Dict[tuple[str, str], int] = {}

    for r in records:
        key = (r.record_id, r.record_type)
        total_retrievals[key] = total_retrievals.get(key, 0) + 1
        if r.outcome is None:
            continue
        if key not in stats:
            stats[key] = {"scored": 0, "success_sum": 0}
        stats[key]["scored"] += 1
        if r.outcome:
            stats[key]["success_sum"] += 1

    candidates: List[DeletionCandidate] = []
    for key, s in stats.items():
        total = total_retrievals.get(key, 0)
        if total < min_retrievals:
            continue
        scored = s["scored"]
        if scored == 0:
            continue
        avg = s["success_sum"] / scored
        if avg < utility_threshold:
            candidates.append(
                DeletionCandidate(
                    record_id=key[0],
                    record_type=key[1],
                    retrieval_count=total,
                    scored_count=scored,
                    avg_utility=avg,
                )
            )

    candidates.sort(key=lambda c: (c.avg_utility, -c.retrieval_count))
    return candidates


def apply_deletions(
    candidates: Sequence[DeletionCandidate],
    record_type: str,
    loader: Any,
    deleter: Any,
) -> List[str]:
    """Execute deletions for one ``record_type`` via caller-provided callbacks.

    ``loader(record_id) -> Any`` fetches the record (returns None if absent).
    ``deleter(record_id) -> bool`` deletes it and returns success.

    This indirection keeps the module free of hard dependencies on specific
    stores (heuristics dir, skills registry, memory plugin). The caller wires
    in the concrete delete for each record type. Returns the list of
    record_ids actually deleted.
    """
    deleted: List[str] = []
    for c in candidates:
        if c.record_type != record_type:
            continue
        try:
            if loader(c.record_id) is None:
                continue
            if deleter(c.record_id):
                deleted.append(c.record_id)
        except Exception:
            continue
    return deleted


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _usage() -> str:
    return (
        "usage: evolution_retrieval_utility.py <command> [options]\n"
        "  analyze [--min-retrievals N] [--threshold F]  Show deletion candidates\n"
        "  log <record_id> [--type T] [--task-key K] [--outcome B]  Log a retrieval\n"
        "  update <task_key> <outcome>  Backfill outcome on pending entries\n"
    )


def main(argv: Optional[List[str]] = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if not args or args[0] in ("--help", "-h"):
        print(_usage())
        return 0

    cmd = args[0]
    rest = args[1:]

    if cmd == "analyze":
        min_r = DEFAULT_MIN_RETRIEVALS
        threshold = DEFAULT_UTILITY_THRESHOLD
        evolution_dir: Optional[Path] = None
        i = 0
        while i < len(rest):
            a = rest[i]
            if a == "--min-retrievals" and i + 1 < len(rest):
                try:
                    min_r = int(rest[i + 1])
                except ValueError:
                    pass
                i += 2
            elif a == "--threshold" and i + 1 < len(rest):
                try:
                    threshold = float(rest[i + 1])
                except ValueError:
                    pass
                i += 2
            elif a == "--evolution-dir" and i + 1 < len(rest):
                evolution_dir = Path(rest[i + 1])
                i += 2
            else:
                i += 1

        records = load_utility_log(evolution_dir)
        candidates = compute_deletion_candidates(
            records, min_retrievals=min_r, utility_threshold=threshold
        )
        print(
            json.dumps(
                {
                    "total_log_entries": len(records),
                    "min_retrievals": min_r,
                    "utility_threshold": threshold,
                    "deletion_candidates": [c.to_dict() for c in candidates],
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    if cmd == "log":
        if not rest:
            print("error: log requires <record_id>", file=sys.stderr)
            return 2
        record_id = rest[0]
        record_type = "heuristic"
        task_key = ""
        outcome: Optional[bool] = None
        i = 1
        while i < len(rest):
            a = rest[i]
            if a == "--type" and i + 1 < len(rest):
                record_type = rest[i + 1]
                i += 2
            elif a == "--task-key" and i + 1 < len(rest):
                task_key = rest[i + 1]
                i += 2
            elif a == "--outcome" and i + 1 < len(rest):
                val = rest[i + 1].lower()
                outcome = (
                    True
                    if val in ("true", "1", "yes")
                    else False
                    if val in ("false", "0", "no")
                    else None
                )
                i += 2
            else:
                i += 1
        path = log_retrieval(record_id, record_type, task_key=task_key, outcome=outcome)
        if path:
            print(f"logged: {path}")
            return 0
        print("error: failed to log", file=sys.stderr)
        return 1

    if cmd == "update":
        if len(rest) < 2:
            print("error: update requires <task_key> <outcome>", file=sys.stderr)
            return 2
        tk = rest[0]
        val = rest[1].lower()
        outcome = val in ("true", "1", "yes", "success")
        n = update_outcome(tk, outcome)
        print(f"updated {n} entries")
        return 0

    print(f"error: unknown command '{cmd}'", file=sys.stderr)
    print(_usage(), file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
