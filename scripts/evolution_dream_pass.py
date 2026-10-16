#!/usr/bin/env python3
"""Grade-weighted dream pass for the evolution pipeline (#1875, child of #1870).

Phase 1 of grade-weighted memory retention ("dreaming"): reads recent cycle
outcomes from ``metrics.jsonl`` and adjusts a file-backed note store so
high-grade runs get promoted (weight raised) and revision-needed runs get a
failure-mode tag. Pure file-based — no LLM, no MCP dependency.

Also hosts slice 1 of issue #48 (Oracular Dream upgrade), REWORK after PR #3110
was sent back by code review. The original scanner read a note store that
does not exist; the dream's real store is **tqmemory** (``~/.turbo-quant-memory/
projects/*/notes/*.json``) — the dream-consolidation skill stores typed notes
there every night at 02:00 and already performs filesystem-level note surgery
(Step 6 episodic housekeeping). This slice points the same deterministic,
conservative pairing logic at that live store and is scheduled as a ``no_agent``
cron job (``evolution-dream-contradiction-scan``, 02:45 daily — see
``cron/evolution/dream-contradiction-scan.yaml``).

Contradiction semantics: two ACTIVE notes with the same normalized title are an
unresolved duplicate/contradiction — the dream's write-time near-duplicate
check (``similar_notes`` → ``supersede_candidate`` → ``deprecate_note``) should
have deprecation-linked the older one. If both are still active, the newer
observation (by ``created_at``) overrides the older, and the older is
deprecation-linked for audit by writing exactly the record tqmemory's
``MemoryStore.deprecate_note`` would write (``note_status=superseded``,
``deprecated_at``, ``deprecation_reason``, ``superseded_by``), so the MCP server
and the filesystem stay consistent. Deterministic, read-only except for the
explicit apply step — no LLM, no embeddings, no MCP dependency.

Entry point for the scheduled pass: ``evolution_dream_contradiction_scan.py``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROMOTE_BUMP = 0.5  # weight increment for high-grade cycles
WEIGHT_CAP = 2.0

# --- tqmemory store contract (mirrors turbo-memory-mcp store.py) -----------
TQMEMORY_DEFAULT_ROOT = Path("~/.turbo-quant-memory").expanduser()
ACTIVE_NOTE_STATUS = "active"
SUPERSEDED_NOTE_STATUS = "superseded"

#: Maximum age of an older note that may be deprecation-linked by the scanner.
#: tqmemory's own lint already suggests deprecating stale episodic notes, and
#: deprecating a years-old durable note purely because a newer same-title note
#: exists is not what this scanner is for — it resolves ACTIVE conflicts within
#: the recent window the dream actually curates. Conservative by design.
MAX_OLDER_AGE_DAYS = 90


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read a JSONL file (one JSON object per line), skipping blank/malformed."""
    out: list[dict[str, Any]] = []
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        s = raw.strip()
        try:
            obj = json.loads(s)
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def load_records(metrics_file: Path) -> list[dict[str, Any]]:
    """Read metrics.jsonl records (one JSON object per line)."""
    return _load_jsonl(metrics_file)


def load_notes(notes_file: Path) -> list[dict[str, Any]]:
    """Read the file-backed note store (one JSON object per line)."""
    return _load_jsonl(notes_file)


def classify_cycle(rec: dict[str, Any]) -> str:
    """high-grade: >=1 merged and ratio >=0.5; revision-needed: 0 merged & >=1
    rejected; else neutral."""
    merged = int(rec.get("merged", 0) or 0)
    selected = int(rec.get("selected", 0) or 0)
    rejected = int(rec.get("rejected", 0) or 0)
    ratio = merged / selected if selected else 0.0
    if merged >= 1 and ratio >= 0.5:
        return "high-grade"
    if merged == 0 and rejected >= 1:
        return "revision-needed"
    return "neutral"


def dream_pass(
    metrics_file: Path, notes_file: Path, *, recent: int = 7
) -> dict[str, Any]:
    """Run one grade-weighted dream pass; write ``dream_pass.json`` summary.

    Promotes notes whose ``cycle`` matches a high-grade record (raise weight);
    tags notes for revision-needed cycles with ``failure:unmerged``.
    """
    records = load_records(metrics_file)[-recent:]
    grades = {r.get("date", "?"): classify_cycle(r) for r in records}
    by_grade = {
        g: [d for d, k in grades.items() if k == g]
        for g in ("high-grade", "revision-needed", "neutral")
    }
    notes = load_notes(notes_file)
    promoted = tagged = 0
    g = by_grade
    for n in notes:
        tags = n.setdefault("tags", [])
        if n.get("cycle") in g["high-grade"]:
            w = min(WEIGHT_CAP, float(n.get("weight", 1.0)) + PROMOTE_BUMP)
            n["weight"] = round(w, 2)
            if "promoted" not in tags:
                tags.append("promoted")
            promoted += 1
        elif n.get("cycle") in g["revision-needed"]:
            if "failure:unmerged" not in tags:
                tags.append("failure:unmerged")
            tagged += 1
    if notes:
        txt = "".join(json.dumps(n) + "\n" for n in notes)
        notes_file.write_text(txt, encoding="utf-8")
    summary = {
        "cycles_reviewed": len(records),
        "high_grade": by_grade["high-grade"],
        "revision_needed": by_grade["revision-needed"],
        "neutral": by_grade["neutral"],
        "notes_promoted": promoted,
        "notes_tagged": tagged,
    }
    (metrics_file.parent / "dream_pass.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return summary


# --- Slice 1 of #48: contradiction scanner over the LIVE tqmemory store ------


def load_tqmemory_notes(
    tqmemory_root: Path | None = None,
) -> list[dict[str, Any]]:
    """Read every note record from the tqmemory filesystem store.

    Layout (as written by turbo-memory-mcp)::

        <root>/projects/<project_id>/notes/<note_id>.json

    Each record keeps its source file path in ``source_path`` so the apply
    step can write the deprecation record back to the exact file the MCP
    server reads. Malformed/unreadable files are skipped — a single corrupt
    note must never take the scheduled pass down.
    """
    root = Path(tqmemory_root or TQMEMORY_DEFAULT_ROOT)
    notes: list[dict[str, Any]] = []
    notes_dir = root / "projects"
    if not notes_dir.is_dir():
        return notes
    for note_file in sorted(notes_dir.glob("*/notes/*.json")):
        try:
            obj = json.loads(note_file.read_text(encoding="utf-8-sig"))
        except (ValueError, OSError):
            continue
        if isinstance(obj, dict) and obj.get("note_id"):
            obj["source_path"] = str(note_file)
            notes.append(obj)
    return notes


def _topic_key(note: dict[str, Any]) -> str | None:
    """Grouping key: normalized ``title`` (lowercased, whitespace-collapsed).

    Notes without a usable title are never grouped — conservatism beats a
    false pairing here (the semantic ``similar_notes`` path at write time
    handles fuzzier matches; this scanner is the deterministic safety net).
    """
    title = note.get("title")
    if not isinstance(title, str) or not title.strip():
        return None
    return " ".join(title.strip().lower().split())


def _created_at_utc(note: dict[str, Any]) -> datetime | None:
    """Parse ``created_at`` as an aware UTC datetime; None when unusable."""
    raw = note.get("created_at")
    if not isinstance(raw, str) or not raw.strip():
        return None
    s = raw.strip()
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:  # naive timestamps are treated as UTC
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _is_active(note: dict[str, Any]) -> bool:
    return note.get("note_status", ACTIVE_NOTE_STATUS) == ACTIVE_NOTE_STATUS


def scan_tqmemory_contradictions(
    notes: list[dict[str, Any]],
    *,
    max_older_age_days: int = MAX_OLDER_AGE_DAYS,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Detect unresolved same-topic active note pairs in the tqmemory store.

    Groups active notes by normalized title. For every group with two or more
    active notes, the NEWEST (by ``created_at``) is authoritative and each
    OLDER active note becomes one proposal: deprecate the older, keep the
    newer. Deterministic, conservative, read-only:

    * only ACTIVE notes participate (already-deprecated/superseded are inert),
    * notes with missing/unparseable ``created_at`` cannot be ordered — skipped,
    * pairs with identical timestamps cannot be ordered — skipped,
    * an older note older than ``max_older_age_days`` is NOT touched (the
      scanner resolves fresh conflicts, not archaeology).

    Returns a list of proposal dicts::

        {
          "topic": "<normalized title>",
          "older_id": "...", "older_created_at": "...", "older_project_id": "...",
          "newer_id": "...", "newer_created_at": "...", "newer_project_id": "...",
          "resolution": "newer-overrides-older",
          "action": "deprecate-older",
        }
    """
    now = now or datetime.now(timezone.utc)
    by_topic: dict[str, list[dict[str, Any]]] = {}
    for n in notes:
        topic = _topic_key(n)
        if topic is None or not _is_active(n):
            continue
        by_topic.setdefault(topic, []).append(n)

    proposals: list[dict[str, Any]] = []
    for topic, group in by_topic.items():
        dated: list[tuple[datetime, dict[str, Any]]] = []
        for n in group:
            ts = _created_at_utc(n)
            if ts is not None:
                dated.append((ts, n))
        if len(dated) < 2:
            continue
        dated.sort(key=lambda pair: pair[0])  # oldest -> newest
        newest_ts, newest = dated[-1]
        for older_ts, older in dated[:-1]:
            if older_ts == newest_ts:
                continue  # no ordering to adjudicate — conservative
            if (now - older_ts).days > max_older_age_days:
                continue  # archaeology, not conflict resolution
            proposals.append({
                "topic": topic,
                "older_id": older["note_id"],
                "older_created_at": older_ts.isoformat(),
                "older_project_id": older.get("project_id"),
                "newer_id": newest["note_id"],
                "newer_created_at": newest_ts.isoformat(),
                "newer_project_id": newest.get("project_id"),
                "resolution": "newer-overrides-older",
                "action": "deprecate-older",
            })
    return proposals


def _utc_now_iso(now: datetime | None) -> str:
    return (
        (now or datetime.now(timezone.utc))
        .astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def apply_tqmemory_deprecations(
    notes: list[dict[str, Any]],
    proposals: list[dict[str, Any]],
    *,
    now: datetime | None = None,
    dry_run: bool = False,
) -> int:
    """Apply deprecation links for the contradiction proposals (issue #48).

    For each proposal, the OLDER active note is flipped to ``superseded`` by
    writing exactly the record ``MemoryStore.deprecate_note`` would write:
    ``note_status``, ``deprecated_at``, ``updated_at``, ``deprecation_reason``
    and a ``superseded_by`` reference to the newer note. The write goes to the
    note's own file (``source_path``), so the MCP server's direct reads see the
    new status immediately; the semantic index catches up on its next
    incremental sync, exactly as with the dream skill's existing filesystem
    housekeeping step.

    Idempotent: a note already superseded/deprecated is skipped. Returns the
    number of notes newly superseded (or that WOULD be, in dry-run).
    """
    by_id = {n.get("note_id"): n for n in notes}
    stamp = _utc_now_iso(now)
    applied = 0
    for prop in proposals:
        older = by_id.get(prop.get("older_id"))
        newer = by_id.get(prop.get("newer_id"))
        if older is None or newer is None:
            continue
        if not _is_active(older) or not _is_active(newer):
            continue  # a previous run (or the write-time path) already handled it
        if dry_run:
            applied += 1
            continue
        older["note_status"] = SUPERSEDED_NOTE_STATUS
        older["deprecated_at"] = stamp
        older["updated_at"] = stamp
        older["deprecation_reason"] = (
            "Contradiction resolution (dream scanner, issue #48): superseded "
            f"by newer same-title note {newer['note_id']}"
        )
        older["superseded_by"] = {
            "scope": newer.get("scope", "project"),
            "project_id": newer.get("project_id"),
            "project_name": newer.get("project_name"),
            "note_id": newer["note_id"],
            "title": newer.get("title"),
            "source_path": newer.get("source_path"),
        }
        src = Path(older["source_path"])
        try:
            src.write_text(
                json.dumps(older, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        except OSError:
            continue  # unreadable store dir: report the failure via the summary
        applied += 1
    return applied


def dream_contradiction_scan(
    tqmemory_root: Path | None = None,
    *,
    dry_run: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run the full contradiction scan over the live tqmemory store.

    Loads every note, detects unresolved same-title active pairs, applies the
    deprecation links (unless ``dry_run``) and returns a summary the scheduled
    runner writes to its report file::

        {
          "scanned_at": "...",
          "notes_scanned": int,
          "active_notes": int,
          "contradictions_found": int,
          "deprecations_applied": int,
          "dry_run": bool,
          "proposals": [...],
        }
    """
    notes = load_tqmemory_notes(tqmemory_root)
    active_before = sum(1 for n in notes if _is_active(n))
    proposals = scan_tqmemory_contradictions(notes, now=now)
    applied = apply_tqmemory_deprecations(notes, proposals, now=now, dry_run=dry_run)
    return {
        "scanned_at": _utc_now_iso(now),
        "tqmemory_root": str(Path(tqmemory_root or TQMEMORY_DEFAULT_ROOT)),
        "notes_scanned": len(notes),
        "active_notes": active_before,
        "contradictions_found": len(proposals),
        "deprecations_applied": applied,
        "dry_run": dry_run,
        "proposals": proposals,
    }


if __name__ == "__main__":  # pragma: no cover
    import sys

    # Original CLI contract preserved: the grade-weighted pass over an
    # evolution data dir (backwards compatible). The scheduled contradiction
    # pass has its own entry point (evolution_dream_contradiction_scan.py).
    evo = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("evolution")
    dream_pass(evo / "metrics.jsonl", evo / "notes.jsonl")
