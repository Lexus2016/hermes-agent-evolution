#!/usr/bin/env python3
"""Experience-bank distillation — cluster harvested failures into global patterns.

Runs as a ``no_agent`` cron job (no LLM, fully deterministic), invoked inline by
the harvest job and callable directly.  Reads the per-session diagnoses the
harvester appended to ``entries.jsonl`` (see ``agent/experience_bank.py`` —
the single source of truth for the schema and storage layout) and rewrites
``patterns.json`` with the distilled, prompt-injectable guidance patterns.

Pipeline:

1. **Evidence selection** — only the latest revision per session is eligible,
   and only entries with ``success is False`` AND
   ``confidence == "high"`` AND ``ts`` inside the 90-day evidence window
   count as failure evidence.  Low-confidence heuristics and unknown
   outcomes (``success is None``) never cluster.
2. **Honest null** — entries with ``primary_dimension is None`` or
   ``failure_category is None`` are counted as ``unattributed`` and never
   become patterns.
3. **Clustering** — group evidence by ``(primary_dimension,
   failure_category, tool)``.  A cluster becomes a pattern only with
   >= 3 entries from >= 3 DISTINCT sessions (evidence diversity: one broken
   cron job repeating must not mint a "global" pattern).
4. **Guidance templates** — only ``(dimension, category)`` pairs with a
   specific, action-oriented template in :data:`GUIDANCE_TEMPLATES` produce
   patterns; anything else is counted ``no_template``.  Never fall back to
   generic advice — that is prompt noise.
5. **Merge + expiry** — matching patterns keep ``first_seen`` and get
   refreshed ``last_seen`` / ``evidence_count`` / guidance; patterns with no
   supporting evidence newer than 30 days are dropped.
6. **Compaction** — ``entries.jsonl`` is atomically rewritten keeping only
   entries inside the 90-day window, bounding unbounded growth.

Concurrency: the shared cross-platform ``.experience.lock`` serializes both
harvest and standalone distillation; a locked run returns immediately with
``{"skipped": "locked"}`` and writes nothing.

Паралельність: спільне міжплатформне блокування ``.experience.lock`` серіалізує
збір і окреме узагальнення; зайняте блокування повертає ``skipped: locked``.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from agent.experience_bank import (
    GUIDANCE_TEMPLATES,
    ExperienceEntry,
    ExperiencePattern,
    entries_path,
    experience_lock,
    iter_entries,
    load_patterns,
    pattern_id,
    save_patterns,
)
#: How far back failure evidence may reach, relative to ``now``.
EVIDENCE_WINDOW_DAYS = 90.0

#: A pattern with no supporting evidence newer than this is dropped.
STALE_PATTERN_DAYS = 30.0

#: A cluster needs at least this many entries...
MIN_EVIDENCE_ENTRIES = 3

#: ...spread across at least this many DISTINCT session ids.
MIN_DISTINCT_SESSIONS = 3

_SECONDS_PER_DAY = 86400.0

# ---------------------------------------------------------------------------
# Entries compaction (bounds entries.jsonl growth)
# ---------------------------------------------------------------------------

def _compact_entries(entries: list[ExperienceEntry], cutoff_ts: float) -> bool:
    """Atomically rewrite ``entries.jsonl`` keeping entries at/after *cutoff_ts*.

    Reads come from the already-parsed *entries* list (via ``iter_entries``),
    re-serialized through ``ExperienceEntry.to_dict`` with the same compact
    separators ``append_entry`` uses, so an idempotent re-run is
    byte-identical.  Missing file: nothing to do.  OSError: warn to stderr
    and keep going — a failed compaction must not kill the cron job.

    Атомарно лишає записи після межі часу. Відсутній файл є успіхом, а OSError
    журналюється без падіння запланованого завдання.
    """
    path = entries_path()
    if not path.exists():
        return True
    kept = [e for e in entries if e.ts >= cutoff_ts]
    try:
        fd, tmp_name = tempfile.mkstemp(
            prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                for entry in kept:
                    fh.write(
                        json.dumps(entry.to_dict(), separators=(",", ":")) + "\n"
                    )
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, path)
            if os.name != "nt":
                dir_fd = os.open(
                    path.parent,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
                )
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        return True
    except OSError as exc:
        print(f"[evolution-distill] failed to compact {path}: {exc}", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# Core distillation
# ---------------------------------------------------------------------------

def _empty_stats() -> dict[str, Any]:
    return {
        "entries_seen": 0,
        "evidence_entries": 0,
        "clusters": 0,
        "patterns_written": 0,
        "patterns_dropped": 0,
        "unattributed": 0,
        "no_template": 0,
        "entries_pruned": 0,
        "write_failures": 0,
    }


def _fill(template: str, tool: Optional[str]) -> Optional[str]:
    """Fill the ``{tool}`` placeholder; None when a tool is required but unknown."""
    if "{tool}" in template:
        if not tool:
            return None
        return template.replace("{tool}", tool)
    return template


def _latest_session_revisions(
    entries: list[ExperienceEntry],
) -> list[ExperienceEntry]:
    """Return only the newest completed revision for each session.

    ``ts`` is the source ``ended_at``; the message id resolves equal-time
    revisions, and append order is the final deterministic tie-breaker.

    Повертає лише найновішу завершену редакцію кожної сесії. Час завершення є
    головним ключем, номер повідомлення та порядок запису розв'язують нічию.
    """
    latest: dict[str, tuple[tuple[float, int, int], ExperienceEntry]] = {}
    for index, entry in enumerate(entries):
        try:
            message_id = int(entry.stats.get("source_last_message_id", 0) or 0)
        except (TypeError, ValueError):
            message_id = 0
        rank = (entry.ts, message_id, index)
        prior = latest.get(entry.session_id)
        if prior is None or rank > prior[0]:
            latest[entry.session_id] = (rank, entry)
    return [latest[session_id][1] for session_id in sorted(latest)]


def _distill(now: float, stats: dict[str, Any]) -> dict[str, Any]:
    evidence_cutoff = now - EVIDENCE_WINDOW_DAYS * _SECONDS_PER_DAY
    stale_cutoff = now - STALE_PATTERN_DAYS * _SECONDS_PER_DAY

    entries = list(iter_entries())
    stats["entries_seen"] = len(entries)
    latest_entries = _latest_session_revisions(entries)

    # 1. Evidence selection: only the latest revision of each session may
    # contribute. / Доказом може бути лише остання редакція кожної сесії.
    evidence = [
        e
        for e in latest_entries
        if e.success is False and e.confidence == "high" and e.ts >= evidence_cutoff
    ]
    stats["evidence_entries"] = len(evidence)

    # 2. Honest null: unattributed evidence never clusters.
    attributable: list[ExperienceEntry] = []
    for e in evidence:
        if e.primary_dimension is None or e.failure_category is None:
            stats["unattributed"] += 1
        else:
            attributable.append(e)

    # 3. Cluster by (dimension, category, tool); tool may be None.
    groups: dict[tuple[str, str, Optional[str]], list[ExperienceEntry]] = {}
    for e in attributable:
        key = (e.primary_dimension or "", e.failure_category or "", e.tool)
        groups.setdefault(key, []).append(e)
    stats["clusters"] = len(groups)

    existing = {p.id: p for p in load_patterns()}
    merged: dict[str, ExperiencePattern] = {}

    # 4. Threshold + template gate; deterministic iteration order.
    for (dimension, category, tool) in sorted(groups, key=lambda k: (k[0], k[1], k[2] or "")):
        members = groups[(dimension, category, tool)]
        distinct_sessions = {m.session_id for m in members}
        if (
            len(members) < MIN_EVIDENCE_ENTRIES
            or len(distinct_sessions) < MIN_DISTINCT_SESSIONS
        ):
            continue
        template = GUIDANCE_TEMPLATES.get((dimension, category))
        if template is None:
            stats["no_template"] += 1
            continue
        trigger = _fill(template[0], tool)
        guidance = _fill(template[1], tool)
        if trigger is None or guidance is None:
            # Template is tool-scoped but this cluster has no known tool.
            stats["no_template"] += 1
            continue

        pid = pattern_id(dimension, category, tool)
        first_seen = min(m.ts for m in members)
        last_seen = max(m.ts for m in members)
        prior = existing.get(pid)
        merged[pid] = ExperiencePattern(
            id=pid,
            dimension=dimension,
            category=category,
            tool=tool,
            trigger=trigger,
            guidance=guidance,
            evidence_count=len(members),
            # 5a. Merge: first_seen survives across runs.
            first_seen=prior.first_seen if prior is not None else first_seen,
            last_seen=last_seen,
        )

    # 5b. Keep still-fresh untouched patterns; drop stale ones.
    for pid, pattern in existing.items():
        if pid in merged:
            continue
        if pattern.last_seen >= stale_cutoff:
            merged[pid] = pattern
        else:
            stats["patterns_dropped"] += 1

    # Stable-diff friendly file: sort by pattern id.
    final = [merged[pid] for pid in sorted(merged)]
    if save_patterns(final):
        stats["patterns_written"] = len(final)
    else:
        stats["write_failures"] += 1

    # 6. Compact the entries log inside the evidence window.
    if _compact_entries(entries, evidence_cutoff):
        stats["entries_pruned"] = stats["entries_seen"] - sum(
            1 for e in entries if e.ts >= evidence_cutoff
        )
    else:
        stats["write_failures"] += 1

    return stats


def run_distillation(
    now: float | None = None,
    *,
    acquire_lock: bool = True,
) -> dict:
    """Cluster failure entries into patterns. Returns stats dict.

    *now* is injectable for tests (defaults to ``time.time()``).  The public
    default acquires the shared bank lock.  The harvester passes
    ``acquire_lock=False`` because it already owns that same lock while calling
    distillation inline.

    *now* можна підмінити в тестах. Публічний виклик отримує спільне блокування,
    а збирач передає ``acquire_lock=False``, бо вже володіє цим блокуванням.
    """
    if now is None:
        now = time.time()
    stats = _empty_stats()
    if not acquire_lock:
        return _distill(now, stats)
    with experience_lock() as acquired:
        if not acquired:
            stats["skipped"] = "locked"
            return stats
        return _distill(now, stats)


def main(argv: Optional[list[str]] = None) -> int:
    """Cron entry point: run once, print the stats as one JSON line."""
    stats = run_distillation()
    print(json.dumps(stats, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
