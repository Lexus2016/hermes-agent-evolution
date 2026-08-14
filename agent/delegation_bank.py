# -*- coding: utf-8 -*-
"""Delegation pattern bank — successful delegation configurations as assets.

Issue #2261 (Slice A of parent #2251 — Mem²Evolve dual asset banks).

The :mod:`agent.experience_bank` harvests *failure* diagnoses into distilled
patterns. This companion module does the symmetric job for *successful
delegation*: it records the configuration used for a ``delegate_task`` call
(goal, context, role, model) along with its outcome, and — when a
configuration consistently succeeds — promotes it to a **retrievable asset**
that can be suggested for similar future tasks.

Design mirrors :mod:`agent.experience_bank`:

* Pure functions + dataclasses; **no side effects on import**.
* Full type hints, ``from __future__ import annotations``.
* JSON serialization (``to_dict`` / ``from_dict``) for every dataclass.
* **No external dependencies** — standard library only.
* **Defensive everywhere** — reads degrade to empty; writes log to stderr
  and swallow OSError; this module is imported on hot code paths.

Storage layout (profile-aware, under ``get_hermes_home()``)::

    <HERMES_HOME>/evolution/delegation/
        records.jsonl   # append-only, one JSON object per delegation
        patterns.json   # promoted successful configurations, rewritten wholesale

The records are **append-only per-session observations**; the patterns are
**distilled reusable assets**. Promotion (record → pattern) is performed by
:func:`promote_patterns`, which a distill cron script calls after harvesting.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence

from hermes_constants import get_hermes_home

__version__ = "1.0.0"

__all__ = [
    "DelegationRecord",
    "DelegationPattern",
    "record_delegation",
    "iter_records",
    "load_delegation_patterns",
    "save_delegation_patterns",
    "promote_patterns",
    "suggest_configurations",
    "format_delegation_suggestions",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: How many supporting successful records a configuration needs before it is
#: promoted to a pattern. Keeps noise out of the asset bank.
_PROMOTION_THRESHOLD = 2

#: Maximum length of stored text fields — guards against pathological inputs
#: bloating the JSONL.
_MAX_TEXT_LEN = 2000

#: Task-type signature length — truncated goal used for matching.
_SIG_LEN = 120

_SECONDS_PER_DAY = 86400.0

# Windows lock-contention errno set (mirrors experience_bank).
_WINDOWS_LOCK_CONTENTION_ERRORS = frozenset({33, 36})


# ---------------------------------------------------------------------------
# Paths (resolved lazily — tests monkeypatch HERMES_HOME)
# ---------------------------------------------------------------------------


def _delegation_dir() -> Path:
    """Return the delegation-bank directory (not created here)."""
    return get_hermes_home() / "evolution" / "delegation"


def records_path() -> Path:
    """Path to the append-only delegation records JSONL."""
    return _delegation_dir() / "records.jsonl"


def patterns_path() -> Path:
    """Path to the promoted delegation patterns JSON."""
    return _delegation_dir() / "patterns.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _warn(message: str) -> None:
    """Log a non-fatal storage problem to stderr. Never raises."""
    try:
        print(f"[delegation_bank] {message}", file=sys.stderr)
    except Exception:  # pragma: no cover - stderr itself is broken
        pass


def _finite_float(value: Any) -> float:
    """Return a finite float, falling back to zero for malformed stored data."""
    try:
        result = float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return result if math.isfinite(result) else 0.0


def _safe_int(value: Any, default: int = 0) -> int:
    """Return an int, falling back to *default* for malformed stored data."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _truncate(value: Any, limit: int) -> str:
    """Safely stringify and truncate a value."""
    s = str(value or "")
    return s[:limit] if len(s) > limit else s


def _task_signature(goal: str) -> str:
    """Derive a compact, normalized task-type signature from a goal string.

    Lowercased, whitespace-collapsed, and truncated — used to cluster
    similar tasks so a proven configuration can be reused.
    """
    normalized = " ".join(goal.lower().split())
    return normalized[:_SIG_LEN]


def _config_hash(
    role: str,
    model: str,
    handoff_mode: Optional[str],
    max_iterations: Optional[int],
) -> str:
    """Stable hash of the delegation configuration (excludes goal/context).

    Two calls with the same orchestration knobs produce the same hash,
    enabling aggregation across different task goals.
    """
    raw = json.dumps(
        {
            "role": role or "leaf",
            "model": model or "",
            "handoff_mode": handoff_mode or "",
            "max_iterations": max_iterations or 0,
        },
        sort_keys=True,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


@contextmanager
def _file_lock() -> Iterator[bool]:
    """Cross-process advisory lock via a lock file (best-effort)."""
    lock_path = _delegation_dir() / ".lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = None
    acquired = False
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
        for _attempt in range(10):
            try:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except (OSError, ImportError):
                time.sleep(0.05)
        if not acquired:
            # Fallback: proceed without lock (append-only writes are tolerant).
            pass
        yield acquired
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def _atomic_write_json(path: Path, payload: Any) -> bool:
    """Write *payload* as JSON to *path* atomically (tmp file + os.replace).

    Returns whether the replacement succeeded. Never raises on OSError.
    Mirrors :func:`agent.experience_bank._atomic_write_json`.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        return True
    except (OSError, TypeError, ValueError) as exc:
        _warn(f"failed to write {path}: {exc}")
        return False


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class DelegationRecord:
    """One observed delegation call with its outcome.

    Attributes:
        v: Schema version.
        ts: Epoch seconds when recorded.
        task_signature: Normalized task-type signature (from the goal).
        goal: The delegation goal (truncated).
        role: Child role (``"leaf"`` or ``"orchestrator"``).
        model: Model identifier used for the child.
        handoff_mode: Handoff mode if set, else ``None``.
        max_iterations: Max iterations if set, else ``None``.
        config_hash: Stable hash of the orchestration configuration.
        success: Whether the delegation completed successfully.
        status: ``"completed"``, ``"failed"``, or ``"interrupted"``.
        duration_seconds: Wall-clock duration of the child run.
        summary: The child's summary output (truncated).
        api_calls: Number of API calls the child made.
        session_id: Parent session identifier.
    """

    v: int = 1
    ts: float = 0.0
    task_signature: str = ""
    goal: str = ""
    role: str = "leaf"
    model: str = ""
    handoff_mode: Optional[str] = None
    max_iterations: Optional[int] = None
    config_hash: str = ""
    success: bool = False
    status: str = "failed"
    duration_seconds: float = 0.0
    summary: str = ""
    api_calls: int = 0
    session_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a JSON-compatible dictionary."""
        return {
            "v": self.v,
            "ts": _finite_float(self.ts),
            "task_signature": self.task_signature,
            "goal": self.goal,
            "role": self.role,
            "model": self.model,
            "handoff_mode": self.handoff_mode,
            "max_iterations": self.max_iterations,
            "config_hash": self.config_hash,
            "success": bool(self.success),
            "status": str(self.status),
            "duration_seconds": _finite_float(self.duration_seconds),
            "summary": self.summary,
            "api_calls": int(self.api_calls or 0),
            "session_id": self.session_id,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "DelegationRecord":
        """Deserialize from a dictionary produced by :meth:`to_dict`.

        Tolerant of malformed field types — coerces to safe defaults.
        """
        return cls(
            v=int(d.get("v", 1) or 1),
            ts=_finite_float(d.get("ts")),
            task_signature=_truncate(d.get("task_signature"), _SIG_LEN),
            goal=_truncate(d.get("goal"), _MAX_TEXT_LEN),
            role=str(d.get("role", "leaf") or "leaf"),
            model=str(d.get("model", "") or ""),
            handoff_mode=(str(d["handoff_mode"]) if d.get("handoff_mode") else None),
            max_iterations=(
                int(d["max_iterations"])
                if d.get("max_iterations")
                and str(d["max_iterations"]).lstrip("-").isdigit()
                else None
            ),
            config_hash=_truncate(d.get("config_hash"), 32),
            success=bool(d.get("success")),
            status=str(d.get("status", "failed") or "failed"),
            duration_seconds=_finite_float(d.get("duration_seconds")),
            summary=_truncate(d.get("summary"), _MAX_TEXT_LEN),
            api_calls=_safe_int(d.get("api_calls", 0)),
            session_id=str(d.get("session_id", "") or ""),
        )


@dataclass
class DelegationPattern:
    """A promoted, reusable delegation configuration.

    Attributes:
        v: Schema version.
        id: Stable slug (``"delg-<config_hash>"``).
        task_signature: The task-type signature this pattern serves.
        goal_example: An example goal for human inspection.
        role: Recommended child role.
        model: Model that succeeded.
        handoff_mode: Recommended handoff mode, or ``None``.
        max_iterations: Recommended max iterations, or ``None``.
        config_hash: Stable hash of the orchestration configuration.
        success_count: Number of successful observations.
        failure_count: Number of failed observations.
        avg_duration_seconds: Mean duration of successful runs.
        last_seen: Epoch seconds of the newest supporting record.
    """

    v: int = 1
    id: str = ""
    task_signature: str = ""
    goal_example: str = ""
    role: str = "leaf"
    model: str = ""
    handoff_mode: Optional[str] = None
    max_iterations: Optional[int] = None
    config_hash: str = ""
    success_count: int = 0
    failure_count: int = 0
    avg_duration_seconds: float = 0.0
    last_seen: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a JSON-compatible dictionary."""
        return {
            "v": self.v,
            "id": self.id,
            "task_signature": self.task_signature,
            "goal_example": self.goal_example,
            "role": self.role,
            "model": self.model,
            "handoff_mode": self.handoff_mode,
            "max_iterations": self.max_iterations,
            "config_hash": self.config_hash,
            "success_count": int(self.success_count),
            "failure_count": int(self.failure_count),
            "avg_duration_seconds": _finite_float(self.avg_duration_seconds),
            "last_seen": _finite_float(self.last_seen),
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "DelegationPattern":
        """Deserialize from a dictionary produced by :meth:`to_dict`.

        Tolerant of malformed field types.
        """
        return cls(
            v=int(d.get("v", 1) or 1),
            id=str(d.get("id", "") or ""),
            task_signature=_truncate(d.get("task_signature"), _SIG_LEN),
            goal_example=_truncate(d.get("goal_example"), _MAX_TEXT_LEN),
            role=str(d.get("role", "leaf") or "leaf"),
            model=str(d.get("model", "") or ""),
            handoff_mode=(str(d["handoff_mode"]) if d.get("handoff_mode") else None),
            max_iterations=(
                int(d["max_iterations"])
                if d.get("max_iterations")
                and str(d["max_iterations"]).lstrip("-").isdigit()
                else None
            ),
            config_hash=_truncate(d.get("config_hash"), 32),
            success_count=_safe_int(d.get("success_count", 0)),
            failure_count=_safe_int(d.get("failure_count", 0)),
            avg_duration_seconds=_finite_float(d.get("avg_duration_seconds")),
            last_seen=_finite_float(d.get("last_seen")),
        )


# ---------------------------------------------------------------------------
# Records (append-only per-delegation observations)
# ---------------------------------------------------------------------------


def record_delegation(record: DelegationRecord) -> bool:
    """Append one delegation record as a JSON line to ``records.jsonl``.

    Automatically fills ``task_signature`` (from ``goal``) and ``config_hash``
    (from ``role``/``model``/``handoff_mode``/``max_iterations``) when the
    caller leaves them empty — so call sites that only know the high-level
    fields (goal, role, model, status) work without pre-hashing.

    Returns whether the write succeeded. Never raises on OSError.
    """
    if not record.task_signature:
        record.task_signature = _task_signature(record.goal)
    if not record.config_hash:
        record.config_hash = _config_hash(
            record.role,
            record.model,
            record.handoff_mode,
            record.max_iterations,
        )
    line = json.dumps(record.to_dict(), ensure_ascii=False)
    if not line.endswith("\n"):
        line += "\n"
    data = line.encode("utf-8")
    path = records_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _file_lock():
            with open(path, "ab") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
        return True
    except OSError as exc:
        _warn(f"failed to append delegation record: {exc}")
        return False


def iter_records(
    since_ts: Optional[float] = None,
) -> Iterator[DelegationRecord]:
    """Yield delegation records, newest-to-oldest, skipping corrupt lines.

    When *since_ts* is given, only records at or after that timestamp are
    yielded.
    """
    path = records_path()
    if not path.exists():
        return
    skipped = 0
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return
    # Newest first.
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
            if not isinstance(d, dict):
                raise ValueError("not a dict")
        except (json.JSONDecodeError, ValueError):
            skipped += 1
            continue
        try:
            rec = DelegationRecord.from_dict(d)
        except (TypeError, ValueError, KeyError):
            skipped += 1
            continue
        if since_ts is not None and rec.ts < since_ts:
            continue
        yield rec
    if skipped:
        _warn(f"skipped {skipped} corrupt delegation record(s) in {path}")


# ---------------------------------------------------------------------------
# Patterns (promoted successful configurations)
# ---------------------------------------------------------------------------


def load_delegation_patterns(
    max_age_days: Optional[float] = None,
) -> List[DelegationPattern]:
    """Load promoted delegation patterns from ``patterns.json``.

    Returns ``[]`` on missing or corrupt file. When *max_age_days* is given,
    patterns whose ``last_seen`` is older are filtered out.
    """
    path = patterns_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except OSError:
        return []
    except ValueError:
        _warn(f"could not parse delegation patterns file {path}; treating as empty")
        return []

    if isinstance(raw, dict):
        raw = raw.get("patterns", [])
    if not isinstance(raw, list):
        return []

    patterns: List[DelegationPattern] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            patterns.append(DelegationPattern.from_dict(item))
        except (TypeError, ValueError, AttributeError):
            continue

    if max_age_days is not None:
        cutoff = time.time() - max_age_days * _SECONDS_PER_DAY
        patterns = [p for p in patterns if p.last_seen >= cutoff]
    return patterns


def save_delegation_patterns(patterns: Sequence[DelegationPattern]) -> bool:
    """Rewrite ``patterns.json`` with the given patterns (atomic write)."""
    payload = [p.to_dict() for p in patterns]
    return _atomic_write_json(patterns_path(), payload)


def promote_patterns(
    since_ts: Optional[float] = None,
    threshold: int = _PROMOTION_THRESHOLD,
) -> List[DelegationPattern]:
    """Aggregate records into patterns and persist successful ones.

    Groups records by ``(task_signature, config_hash)``. A group is promoted
    to a :class:`DelegationPattern` only when its success count meets
    *threshold*. Patterns with only failures are dropped (not stored).

    Returns the list of promoted patterns.
    """
    groups: Dict[str, List[DelegationRecord]] = {}
    for rec in iter_records(since_ts=since_ts):
        if not rec.config_hash or not rec.task_signature:
            continue
        key = f"{rec.task_signature}::{rec.config_hash}"
        groups.setdefault(key, []).append(rec)

    patterns: List[DelegationPattern] = []
    for _key, recs in groups.items():
        successes = [r for r in recs if r.success]
        failures = [r for r in recs if not r.success]
        if len(successes) < threshold:
            continue
        # Use the most recent successful record as the exemplar.
        exemplar = max(successes, key=lambda r: r.ts)
        durations = [_finite_float(r.duration_seconds) for r in successes]
        avg_dur = sum(durations) / len(durations) if durations else 0.0
        patterns.append(
            DelegationPattern(
                id=f"delg-{exemplar.config_hash}",
                task_signature=exemplar.task_signature,
                goal_example=exemplar.goal,
                role=exemplar.role,
                model=exemplar.model,
                handoff_mode=exemplar.handoff_mode,
                max_iterations=exemplar.max_iterations,
                config_hash=exemplar.config_hash,
                success_count=len(successes),
                failure_count=len(failures),
                avg_duration_seconds=avg_dur,
                last_seen=max(r.ts for r in recs),
            )
        )

    patterns.sort(key=lambda p: (-p.success_count, -p.last_seen))
    save_delegation_patterns(patterns)
    return patterns


# ---------------------------------------------------------------------------
# Retrieval — suggest proven configurations for a new task
# ---------------------------------------------------------------------------


def suggest_configurations(
    goal: str,
    *,
    max_suggestions: int = 3,
    min_similarity: int = 10,
) -> List[DelegationPattern]:
    """Suggest proven delegation configurations for a new task goal.

    Ranks stored patterns by substring/word overlap between the goal's
    signature and the pattern's ``task_signature``. Only patterns whose
    similarity meets *min_similarity* (in shared characters) are returned.

    Returns at most *max_suggestions* patterns, best match first.
    """
    sig = _task_signature(goal)
    if not sig:
        return []
    patterns = load_delegation_patterns()
    if not patterns:
        return []

    scored: List[tuple[int, DelegationPattern]] = []
    sig_words = set(sig.split())
    for p in patterns:
        # Word-overlap score.
        p_words = set(p.task_signature.split())
        common = sig_words & p_words
        if not common:
            # Fall back to substring containment.
            if p.task_signature and p.task_signature in sig:
                score = len(p.task_signature)
            elif sig and sig in p.task_signature:
                score = len(sig) // 2
            else:
                continue
        else:
            score = sum(len(w) for w in common)
        if score >= min_similarity:
            scored.append((score, p))

    scored.sort(key=lambda sp: (-sp[0], -sp[1].success_count))
    return [p for _, p in scored[:max_suggestions]]


def format_delegation_suggestions(goal: str) -> str:
    """Render a compact text block of proven delegation suggestions for *goal*.

    Returns ``""`` when there are no suggestions. Intended for injection
    into the delegate_task tool result or a planning prompt.
    """
    suggestions = suggest_configurations(goal)
    if not suggestions:
        return ""
    lines = ["## Proven delegation configurations for similar tasks:"]
    for p in suggestions:
        parts = [f"role={p.role}"]
        if p.model:
            parts.append(f"model={p.model}")
        if p.handoff_mode:
            parts.append(f"handoff={p.handoff_mode}")
        lines.append(
            f"- {', '.join(parts)} "
            f"(succeeded {p.success_count}×, avg {p.avg_duration_seconds:.0f}s)"
        )
    return "\n".join(lines)
