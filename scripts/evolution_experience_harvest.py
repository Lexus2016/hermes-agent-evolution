#!/usr/bin/env python3
"""Experience-bank harvest — per-session execution diagnoses (no LLM).

Runs as a ``no_agent`` cron job (deterministic, no model calls).  Scans the
SQLite SessionDB (``hermes_state.py``) for sessions that finished since the
last run and appends ONE :class:`ExperienceEntry` per completed session revision to
``<HERMES_HOME>/evolution/experience/entries.jsonl`` — the per-case layer of
the MemoHarness-inspired experience bank (see ``agent/experience_bank.py``,
which owns the schema).  When the bank contains entries, distillation is
invoked inline at the end (sibling ``evolution_experience_distill.py``), so a
previous distillation failure heals even when the current pass appends nothing.

Design-review constraints honored here:

* **Strong-signal-only failure verdicts.** ``success=False`` is emitted only
  for failure modes with real persisted evidence (loop-guard hard stop,
  max-iteration exhaustion, unhandled processing error — see the marker
  constants below), always with ``confidence="high"`` and an
  ``outcome_source`` naming the heuristic.  Everything else is
  ``success=None`` (unknown) or a low-confidence clean-completion heuristic.
* **Honest-null dimension attribution.**  Unambiguous failure categories get a
  ``primary_dimension``.  A timeout from a concrete named tool is attributed to
  ``tool``; a timeout with no trustworthy tool name remains honest-null.
* **Controlled-vocabulary analysis.**  The ``analysis`` field is built
  exclusively from tool names (character-scrubbed), FailureCategory value
  strings, and integer counts — raw user/model/tool text is NEVER copied
  into an entry (anti-injection + anti-secret-leak).
* **One cron job + shared lock.**  A non-blocking cross-platform lock guards
  both harvest and distillation; distillation rides on this job, not a second
  one.

Failure detection matches Hermes' own markers: tool-error results are found
with the SAME predicate the live agent loop uses
(``agent.display._detect_tool_failure``), and categories come from
``evolution.lib.root_cause_diagnosis.ErrorClassifier`` — nothing invented
here.

Only rows with ``ended_at`` are eligible.  Dedup tracks completed revisions
(``session_id`` + ``ended_at`` + maximum ``messages.id``), not bare
session ids.  The state keeps monotonic time and message-id cursors, allowing a
reopened session or a late historical import to be harvested exactly once.

Обробляються лише рядки з ``ended_at``. Завершена редакція визначається
ідентифікатором сесії, часом завершення та найбільшим номером повідомлення.
Це дає змогу один раз зібрати повторно завершену сесію або пізній імпорт.

Exit codes: 0 always for the lock-contention no-op and clean runs; 1 only
for an unexpected top-level failure (the cron scheduler surfaces it).
"""

from __future__ import annotations

import json
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

# Add the scripts/ dir (sibling-script imports like evolution_experience_distill)
# AND the repo root (parent of scripts/) so the `evolution` namespace package
# resolves when the script is run in-repo from any cwd.  When the script is
# installed into HERMES_HOME/scripts (outside the repo), `evolution` is not
# present — the graceful fallback below keeps the harvest running instead of
# crashing every cron tick (mirrors evolution_watchdog.py's ImportError fallback).
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.display import _detect_tool_failure  # noqa: E402
from agent.experience_bank import (  # noqa: E402
    CATEGORY_TO_DIMENSION,
    ExperienceEntry,
    append_entry,
    experience_lock,
    get_harvest_state,
    iter_entries,
    set_harvest_state,
)

try:
    from evolution.lib.root_cause_diagnosis import ErrorClassifier  # noqa: E402
except ImportError:
    import enum  # noqa: E402

    class _FallbackFailureCategory(enum.Enum):
        """Minimal stand-in for FailureCategory when `evolution` is absent."""

        UNKNOWN = "unknown"

    class ErrorClassifier:  # type: ignore[no-redef]
        """Degraded fallback used when the `evolution` package is unavailable.

        ``classify`` always returns ``UNKNOWN`` so the harvest still emits
        valid entries (``failure_category="unknown"``) and distillation can
        proceed, instead of the cron failing every tick with
        ``ModuleNotFoundError``.  See GitHub issue #1304.
        """

        def classify(self, content: str):  # noqa: D401, ARG002
            return _FallbackFailureCategory.UNKNOWN

    print(
        "[experience-harvest] evolution.lib.root_cause_diagnosis unavailable "
        "— using UNKNOWN fallback (run from the repo root or install the "
        "evolution package for full failure classification).",
        file=sys.stderr,
    )
from hermes_constants import get_hermes_home  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Look-behind overlap so a session that straddles the previous cursor is
#: re-examined (dedup happens via seen_ids).
_CURSOR_OVERLAP_S = 15 * 60.0

#: Cap on the dedup id list kept in harvest_state.json.
_SEEN_IDS_CAP = 5000

#: State schema marker written only after the one-time legacy revision scan.
_REVISION_STATE_VERSION = 1

#: Strong failure markers, mirrored from the agent loop (do not paraphrase —
#: these strings are what actually lands in persisted session messages):
#: * ``agent/conversation_loop.py`` loop-guard hard stops append a final
#:   assistant message starting with ``[loop-guard] ``
#:   (turn_exit_reason loop_guard_{cron,interactive}_hard_stop).
#: * ``agent/conversation_loop.py`` local-processing / repeated-error exits
#:   append a final assistant message starting with the apology prefix
#:   (turn_exit_reason local_processing_error(...) /
#:   error_near_max_iterations(...)).
#: * ``agent/chat_completion_helpers.handle_max_iterations`` appends a
#:   synthetic user message with the max-iterations summary request.
_LOOP_GUARD_PREFIX = "[loop-guard] "
_APOLOGY_PREFIX = "I apologize, but I encountered"
_MAX_ITER_MARKER = "You've reached the maximum number of tool-calling iterations"

#: Categories whose CATEGORY_TO_DIMENSION assignment is unambiguous — only
#: these may set primary_dimension (design-review honest-null rule).
_UNAMBIGUOUS_CATEGORIES = frozenset(
    {"network", "permission", "not_found", "syntax_error"}
)

#: Candidate dimension lists for the ambiguous categories; primary stays None.
_AMBIGUOUS_SECONDARY: Dict[str, List[str]] = {
    "validation": ["tool", "output"],
    "timeout": ["tool", "generation", "orchestration"],
    "resource_limit": ["context", "generation", "tool"],
}

#: Analysis-field vocabulary scrub: anything outside this alphabet in a tool
#: name is folded to "_" so the analysis string can never carry raw text.
_TOOL_NAME_SAFE = set("abcdefghijklmnopqrstuvwxyz0123456789_")


# ---------------------------------------------------------------------------
# Session DB access
# ---------------------------------------------------------------------------

def _default_db_path() -> Path:
    """Resolve the SessionDB path lazily (tests monkeypatch HERMES_HOME)."""
    return get_hermes_home() / "state.db"


def _iter_session_rows(
    db: Any,
    cursor_ts: float = 0.0,
    last_message_id: int = 0,
    *,
    full_scan: bool = False,
) -> List[Dict[str, Any]]:
    """Return completed session rows in monotonic completion order.

    Uses a single aggregate query instead of list_sessions_rich so child
    (subagent) sessions are included and no compression projection hides
    rows — the harvester wants EVERY session, not a user-facing listing.

    Повертає завершені сесії у монотонному порядку завершення, включно з
    дочірніми сесіями, без проєкції для інтерфейсу користувача.
    """
    sql = (
        "SELECT s.id, s.source, s.model, s.started_at, s.ended_at, "
        "s.end_reason, s.message_count, s.tool_call_count, "
        "COALESCE(MAX(m.id), 0) "
        "AS source_last_message_id "
        "FROM sessions s "
        "LEFT JOIN messages m ON m.session_id = s.id "
        "WHERE s.ended_at IS NOT NULL "
        "GROUP BY s.id "
    )
    params: tuple[Any, ...] = ()
    if not full_scan:
        sql += "HAVING s.ended_at > ? OR source_last_message_id > ? "
        params = (cursor_ts - _CURSOR_OVERLAP_S, last_message_id)
    sql += "ORDER BY s.ended_at ASC, s.id ASC"
    with db._lock:
        rows = db._conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def _session_snapshot(db: Any, session_id: str) -> Optional[tuple[float, int]]:
    """Return the current completed-session snapshot, or None if reopened.

    Повертає поточний знімок завершеної сесії або ``None``, якщо її відкрили.
    """
    sql = (
        "SELECT s.ended_at, "
        "COALESCE(MAX(m.id), 0) "
        "AS source_last_message_id "
        "FROM sessions s LEFT JOIN messages m ON m.session_id = s.id "
        "WHERE s.id = ? GROUP BY s.id"
    )
    with db._lock:
        row = db._conn.execute(sql, (session_id,)).fetchone()
    if row is None or row["ended_at"] is None:
        return None
    return float(row["ended_at"]), int(row["source_last_message_id"])


def _revision_key(session_id: str, ended_at: float, last_message_id: int) -> str:
    """Serialize a completed-session revision into a stable state key."""
    return json.dumps(
        [session_id, float(ended_at), int(last_message_id)],
        separators=(",", ":"),
    )


def _entry_message_id(entry: ExperienceEntry) -> Optional[int]:
    """Return a valid stored source message id, or None for legacy/corrupt data.

    Повертає коректний номер повідомлення або ``None`` для старих чи
    пошкоджених даних.
    """
    raw = entry.stats.get("source_last_message_id")
    if raw is None:
        return None
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Per-session analysis
# ---------------------------------------------------------------------------

def _scrub_tool_name(name: Any) -> str:
    """Reduce a tool name to the controlled-vocabulary alphabet."""
    cleaned = "".join(
        ch if ch in _TOOL_NAME_SAFE else "_" for ch in str(name or "").lower()
    )
    return cleaned.strip("_") or "unknown"


def _message_text(msg: Dict[str, Any]) -> str:
    """Return deterministic text for string or structured message content.

    Повертає детермінований текст для рядкового або структурованого вмісту.
    """
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, (dict, list)):
        try:
            return json.dumps(content, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError):
            return ""
    return ""


def analyze_session(messages: List[Dict[str, Any]], ended: bool) -> Dict[str, Any]:
    """Deterministically analyze one session's messages.

    Args:
        messages: Active messages in insertion order (SessionDB.get_messages).
        ended: Whether the session row has ``ended_at`` set (a session that
            was never ended and is no longer active counts as interrupted).

    Returns a dict with the verdict fields ready to feed ExperienceEntry:
    ``success``, ``outcome_source``, ``confidence``, ``terminal_reason``,
    ``primary_dimension``, ``secondary_dimensions``, ``failure_category``,
    ``tool``, ``analysis``, ``stats``, plus ``has_unrecovered`` (internal —
    used by the caller's nothing-to-learn skip rule).
    """
    # ── Tool-error tallies, using Hermes' own failure predicate ──
    # An error is UNRECOVERED only when the same tool never succeeds later
    # in the session; recovered errors are normal agent behavior.
    tool_msgs: List[Dict[str, Any]] = [
        m for m in messages if m.get("role") == "tool"
    ]
    last_success_idx: Dict[str, int] = {}
    failures: List[tuple] = []  # (index, tool, category)
    for idx, msg in enumerate(tool_msgs):
        tool = _scrub_tool_name(msg.get("tool_name"))
        content = _message_text(msg)
        is_error, _suffix = _detect_tool_failure(
            str(msg.get("tool_name") or ""), content
        )
        if is_error:
            category = ErrorClassifier().classify(content)
            failures.append((idx, tool, category.value))
        else:
            last_success_idx[tool] = idx

    unrecovered: Counter = Counter()   # (tool, category) -> count
    recovered_per_tool: Counter = Counter()
    first_seen_order: List[tuple] = []
    for idx, tool, category in failures:
        if idx > last_success_idx.get(tool, -1):
            key = (tool, category)
            if key not in unrecovered:
                first_seen_order.append(key)
            unrecovered[key] += 1
        else:
            recovered_per_tool[tool] += 1

    total_unrecovered = sum(unrecovered.values())
    total_recovered = sum(recovered_per_tool.values())

    # Dominant unrecovered (tool, category): most occurrences, ties broken by
    # first occurrence order for full determinism.
    dominant_tool: Optional[str] = None
    dominant_category: Optional[str] = None
    if unrecovered:
        dominant_key = max(
            first_seen_order,
            key=lambda k: unrecovered[k],
        )
        dominant_tool, dominant_category = dominant_key

    # ── Strong failure signals (persisted, high-confidence markers only) ──
    final_assistant = ""
    for msg in reversed(messages):
        if msg.get("role") == "assistant":
            final_assistant = _message_text(msg).strip()
            break
    hit_max_iterations = False
    if messages:
        last_idx = len(messages) - 1
        for idx, message in enumerate(messages):
            if (
                message.get("role") == "user"
                and _MAX_ITER_MARKER in _message_text(message)
                and (
                    idx == last_idx
                    or (
                        idx == last_idx - 1
                        and messages[last_idx].get("role") == "assistant"
                    )
                )
            ):
                hit_max_iterations = True
                break

    success: Optional[bool] = None
    outcome_source = ""
    confidence = "low"
    if final_assistant.startswith(_LOOP_GUARD_PREFIX):
        success, confidence = False, "high"
        outcome_source = "heuristic:loop_guard_hard_stop"
        terminal_reason = "loop_guard_hard_stop"
    elif final_assistant.startswith(_APOLOGY_PREFIX):
        success = False
        confidence = "high" if total_unrecovered > 0 else "low"
        outcome_source = "heuristic:unhandled_exception"
        terminal_reason = "unhandled_exception"
    elif hit_max_iterations:
        success, confidence = False, "high"
        outcome_source = "heuristic:max_iterations_exhausted"
        terminal_reason = "iteration_exhausted"
    else:
        terminal_reason = "completed" if ended else "interrupted"
        if ended and final_assistant and total_unrecovered == 0:
            # Clean completion heuristic: ended session, final assistant
            # reply, zero unrecovered tool errors.
            success = True
            outcome_source = "heuristic:clean_completion"

    # ── Honest-null dimension attribution (dominant unrecovered error) ──
    primary_dimension: Optional[str] = None
    secondary_dimensions: List[str] = []
    if dominant_category in _UNAMBIGUOUS_CATEGORIES:
        primary_dimension = CATEGORY_TO_DIMENSION.get(dominant_category)
    elif dominant_category == "timeout" and dominant_tool != "unknown":
        # A timeout from a concrete tool is actionable tool evidence; an
        # unknown tool stays honest-null. / Таймаут конкретного інструмента є
        # доказом для виміру tool; невідомий інструмент лишається без атрибуції.
        primary_dimension = "tool"
        secondary_dimensions = ["generation", "orchestration"]
    elif dominant_category in _AMBIGUOUS_SECONDARY:
        secondary_dimensions = list(_AMBIGUOUS_SECONDARY[dominant_category])
    # "unknown" (and no unrecovered errors) -> both stay empty/None.

    # ── Controlled-vocabulary analysis field ──
    # Built ONLY from scrubbed tool names, FailureCategory value strings and
    # integer counts — raw user/model/tool text never reaches the entry.
    # tool/category name the DOMINANT unrecovered pair (none when there are
    # no unrecovered errors); the counts are session-wide totals.
    analysis = (
        f"tool={dominant_tool or 'none'} "
        f"category={dominant_category or 'none'} "
        f"unrecovered={total_unrecovered} "
        f"recovered={total_recovered}"
    )

    return {
        "success": success,
        "outcome_source": outcome_source,
        "confidence": confidence,
        "terminal_reason": terminal_reason,
        "primary_dimension": primary_dimension,
        "secondary_dimensions": secondary_dimensions,
        "failure_category": dominant_category,
        "tool": dominant_tool,
        "analysis": analysis,
        "stats": {
            "tool_calls": len(tool_msgs),
            "tool_errors": len(failures),
            "iterations": sum(1 for m in messages if m.get("role") == "assistant"),
            "unrecovered_errors": total_unrecovered,
            "recovered_errors": total_recovered,
        },
        "has_unrecovered": total_unrecovered > 0,
    }


# ---------------------------------------------------------------------------
# Harvest
# ---------------------------------------------------------------------------

def _maybe_distill() -> Optional[Dict[str, Any]]:
    """Invoke distillation inline (sibling script).  Never raises."""
    try:
        from evolution_experience_distill import run_distillation

        result = run_distillation(acquire_lock=False)
        return result if isinstance(result, dict) else {"result": str(result)}
    except Exception as exc:
        print(
            f"[experience-harvest] distillation failed (non-fatal): {exc}",
            file=sys.stderr,
        )
        return None


def run_harvest(
    db_path: Optional[Path] = None,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """Run one harvest pass.  Returns the summary dict main() prints.

    Import-safe and unit-testable: *db_path* / *now* inject the seams;
    distillation is invoked inline whenever the bank contains entries.
    """
    from hermes_state import SessionDB

    now = time.time() if now is None else float(now)
    db_path = db_path or _default_db_path()

    summary: Dict[str, Any] = {
        "sessions_scanned": 0,
        "sessions_harvested": 0,
        "entries_appended": 0,
        "write_failures": 0,
        "state_saved": True,
        "distill": None,
    }
    if not db_path.exists():
        return summary

    state = get_harvest_state()
    try:
        cursor_ts = float(state.get("cursor_ts", 0.0) or 0.0)
    except (TypeError, ValueError):
        cursor_ts = 0.0
    try:
        last_message_id = int(state.get("last_message_id", 0) or 0)
    except (TypeError, ValueError):
        last_message_id = 0
    seen_ids: List[str] = [
        str(s) for s in (state.get("seen_ids") or []) if isinstance(s, str)
    ]
    seen_set = set(seen_ids)
    seen_revisions: List[str] = [
        str(value)
        for value in (state.get("seen_revisions") or [])
        if isinstance(value, str)
    ]
    seen_revision_set = set(seen_revisions)
    try:
        revision_state_version = int(state.get("revision_state_version", 0) or 0)
    except (TypeError, ValueError):
        revision_state_version = 0
    migration_needed = revision_state_version < _REVISION_STATE_VERSION
    existing_entries = list(iter_entries())
    existing_revision_keys = set()
    legacy_entry_revisions = set()
    for entry in existing_entries:
        entry_message_id = _entry_message_id(entry)
        if entry_message_id is None:
            legacy_entry_revisions.add((entry.session_id, entry.ts))
        else:
            existing_revision_keys.add(
                _revision_key(entry.session_id, entry.ts, entry_message_id)
            )
    # Entries written before revision tracking can still heal a failed state
    # write when their session and ended_at match. / Старі записи без номера
    # повідомлення дедуплікуються за сесією та часом завершення.

    db = SessionDB(db_path=db_path, read_only=True)
    try:
        # Legacy state cannot identify which completion a bare seen_id covered.
        # Scan all ended rows once, then mark the revision migration complete.
        # Старий seen_id не визначає конкретне завершення, тому один раз
        # переглядаємо всі завершені рядки й лише потім завершуємо міграцію.
        rows = _iter_session_rows(
            db,
            cursor_ts,
            last_message_id,
            full_scan=migration_needed,
        )

        processed_max_ts = cursor_ts
        processed_max_message_id = last_message_id
        new_seen_revisions: List[str] = []
        new_seen_ids: List[str] = []
        migration_pass_complete = True
        for row in rows:
            summary["sessions_scanned"] += 1
            session_id = str(row.get("id") or "")
            if not session_id:
                continue
            ended_at = float(row["ended_at"])
            source_last_message_id = int(row["source_last_message_id"])
            revision_key = _revision_key(
                session_id, ended_at, source_last_message_id
            )
            if revision_key in seen_revision_set:
                processed_max_ts = max(processed_max_ts, ended_at)
                processed_max_message_id = max(
                    processed_max_message_id, source_last_message_id
                )
                continue
            # Heal a prior state-write failure without duplicating the entry.
            # Відновити стан після помилки запису без дублювання редакції.
            if (
                revision_key in existing_revision_keys
                or (session_id, ended_at) in legacy_entry_revisions
            ):
                processed_max_ts = max(processed_max_ts, ended_at)
                processed_max_message_id = max(
                    processed_max_message_id, source_last_message_id
                )
                new_seen_revisions.append(revision_key)
                new_seen_ids.append(session_id)
                continue

            messages = db.get_messages(session_id)
            verdict = analyze_session(messages, ended=True)
            # Reopen/append race guard: never mark a snapshot that changed
            # while messages were being read. / Не позначати знімок, який
            # змінився під час читання повідомлень.
            if _session_snapshot(db, session_id) != (
                ended_at,
                source_last_message_id,
            ):
                migration_pass_complete = False
                continue

            # Nothing-to-learn skip: no unrecovered errors AND no verdict
            # either way (success=None).  Clean successes (True) and every
            # failure verdict (False) ARE appended.
            if not verdict["has_unrecovered"] and verdict["success"] is None:
                summary["sessions_harvested"] += 1
                processed_max_ts = max(processed_max_ts, ended_at)
                processed_max_message_id = max(
                    processed_max_message_id, source_last_message_id
                )
                new_seen_revisions.append(revision_key)
                new_seen_ids.append(session_id)
                continue

            verdict["stats"]["source_last_message_id"] = source_last_message_id
            entry = ExperienceEntry(
                ts=ended_at,
                session_id=session_id,
                platform=str(row.get("source") or ""),
                model=str(row.get("model") or ""),
                success=verdict["success"],
                outcome_source=verdict["outcome_source"],
                confidence=verdict["confidence"],
                terminal_reason=verdict["terminal_reason"],
                primary_dimension=verdict["primary_dimension"],
                secondary_dimensions=verdict["secondary_dimensions"],
                failure_category=verdict["failure_category"],
                tool=verdict["tool"],
                analysis=verdict["analysis"],
                stats=verdict["stats"],
            )
            if not append_entry(entry):
                summary["write_failures"] += 1
                migration_pass_complete = False
                break
            summary["sessions_harvested"] += 1
            summary["entries_appended"] += 1
            processed_max_ts = max(processed_max_ts, ended_at)
            processed_max_message_id = max(
                processed_max_message_id, source_last_message_id
            )
            new_seen_revisions.append(revision_key)
            new_seen_ids.append(session_id)

        # Persist both monotonic cursors and exact completed revisions.
        # Зберегти обидва монотонні курсори та точні завершені редакції.
        state_changed = (
            bool(new_seen_revisions)
            or migration_needed
            or processed_max_ts != cursor_ts
            or processed_max_message_id != last_message_id
        )
        if state_changed:
            merged_seen = seen_ids + [
                s for s in new_seen_ids if s not in seen_set
            ]
            merged_revisions = seen_revisions + [
                key for key in new_seen_revisions if key not in seen_revision_set
            ]
            next_state = {
                "cursor_ts": processed_max_ts,
                "last_message_id": processed_max_message_id,
                "seen_ids": merged_seen[-_SEEN_IDS_CAP:],
                "seen_revisions": merged_revisions[-_SEEN_IDS_CAP:],
            }
            if not migration_needed or migration_pass_complete:
                next_state["revision_state_version"] = _REVISION_STATE_VERSION
            summary["state_saved"] = set_harvest_state(next_state)
    finally:
        try:
            db.close()
        except Exception:
            pass

    if existing_entries or summary["entries_appended"] > 0:
        summary["distill"] = _maybe_distill()

    return summary


# ---------------------------------------------------------------------------
# Lock + main
# ---------------------------------------------------------------------------

def main(argv: List[str]) -> int:
    with experience_lock() as acquired:
        if not acquired:
            print(
                "[experience-harvest] another bank operation is running; exiting / "
                "інша операція банку вже виконується; вихід"
            )
            return 0
        summary = run_harvest()
    # Deterministic no_agent job: one compact JSON summary line so the run
    # log shows what happened; empty work still prints zeros.
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
