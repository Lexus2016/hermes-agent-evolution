#!/usr/bin/env python3
"""Conflict-preserving memory detection for tqmemory notes (#908).

Hermes's memory (``MEMORY.md``, ``USER.md``, structured memories) can
accumulate *contradictory* claims across sessions — one session records
"the deploy target is staging", a later one records "the deploy target is
production", and both entries sit side by side with no indication that they
disagree. The built-in memory store's ``add()`` is append-only (it never
overwrites), so nothing is silently lost — but nothing surfaces the
disagreement either, and an agent reading the corpus has no way to tell that
two entries are in conflict rather than simply about related topics.

This module is a *side-effect-free* analysis pass, mirroring
``agent/memory_staleness.py``: it never mutates the notes it inspects and
never calls any memory API. It detects pairs of notes that make a claim
about the same *topic* but disagree on the *value*, and returns a
:class:`ConflictReport` — an explicit, queryable record of the disagreement
— that the caller surfaces (CLI report, system-prompt note, etc.) rather
than silently picking a winner.

Detection is a deterministic heuristic, not semantic understanding:

1. Each note's title is split into a ``(topic, value)`` pair on the first
   matching separator (``:``, ``" is "``, ``" are "``, ``"="``,
   ``" prefers "``) via :func:`split_claim`. Notes whose title has no
   recognizable separator are not claims and are skipped — this mirrors
   ``detect_contradictions`` in ``memory_staleness.py`` skipping notes
   without timestamps: the detector only operates on notes it can
   structurally interpret.
2. Two claims *conflict* when their topics are near-identical (Jaccard
   word-set similarity at/above ``topic_similarity_threshold``) but their
   values are substantially different (Jaccard word-set similarity *below*
   ``value_similarity_threshold``). Same topic + same value is a duplicate
   (out of scope here — see ``detect_duplicates``); same topic + different
   value is a conflict.

The module depends only on the Python standard library (plus reusing the
``Note`` model and ``jaccard_similarity`` helper from ``memory_staleness``),
is import-safe, and is unit-testable in isolation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Optional, Tuple

from agent.memory_staleness import Note, jaccard_similarity

__all__ = [
    "Note",
    "MemoryConflict",
    "ConflictReport",
    "DEFAULT_CONFIG",
    "split_claim",
    "detect_conflicts",
    "analyze_conflicts",
    "render_conflict_report",
]


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

#: Default configuration. Values are intentionally conservative so the
#: analysis is useful out-of-the-box; every threshold is overridable via
#: :func:`analyze_conflicts`'s ``config`` argument.
DEFAULT_CONFIG: dict[str, Any] = {
    # Topic word-set Jaccard similarity at/above this fraction means "same topic".
    "topic_similarity_threshold": 0.6,
    # Value word-set Jaccard similarity below this fraction means "different
    # value" (a same-topic pair at/above this is a near-duplicate claim, not
    # a conflict — that's DUPLICATE territory, handled by memory_staleness).
    "value_similarity_threshold": 0.5,
}


# ---------------------------------------------------------------------------
# Claim splitting
# ---------------------------------------------------------------------------

# Ordered by priority: the first pattern that splits the title into two
# non-empty parts wins. Colon-delimited claims ("Deploy target: staging")
# are the most common and least ambiguous form in real memory entries, so
# they take priority over the looser natural-language separators.
_SEPARATOR_PATTERNS: Tuple[re.Pattern, ...] = (
    re.compile(r"\s*:\s*"),
    re.compile(r"\s+is\s+", re.IGNORECASE),
    re.compile(r"\s+are\s+", re.IGNORECASE),
    re.compile(r"\s*=\s*"),
    re.compile(r"\s+prefers\s+", re.IGNORECASE),
)

_WORD_RE = re.compile(r"[^a-z0-9]+")


def _word_set(text: str) -> set[str]:
    """Normalized word set for Jaccard comparison (lowercase, alnum runs)."""
    return {w for w in _WORD_RE.split(text.lower()) if w}


def split_claim(text: str) -> Optional[Tuple[str, str]]:
    """Split ``text`` into a ``(topic, value)`` pair on the first matching separator.

    Tries each separator in :data:`_SEPARATOR_PATTERNS` in priority order and
    returns the first split that yields two non-empty, stripped parts.
    Returns ``None`` when no separator matches (or a match produces an empty
    topic or value) — the text is not a recognizable key/value claim.
    """
    for pattern in _SEPARATOR_PATTERNS:
        parts = pattern.split(text, maxsplit=1)
        if len(parts) != 2:
            continue
        topic, value = parts[0].strip(), parts[1].strip()
        if topic and value:
            return topic, value
    return None


# ---------------------------------------------------------------------------
# Conflict model
# ---------------------------------------------------------------------------


@dataclass
class MemoryConflict:
    """A pair of memory entries that claim different values for the same topic.

    Both notes are left untouched by this module — the conflict is a
    *finding*, not a mutation. ``note_a_id``/``note_b_id`` are always ordered
    so results are deterministic regardless of input or traversal order (the
    lexicographically smaller id is always ``note_a_id``).

    Attributes:
        note_a_id: The first note's id (lexicographically smaller of the pair).
        note_b_id: The second note's id.
        topic_a: The topic phrase extracted from ``note_a``'s claim.
        topic_b: The topic phrase extracted from ``note_b``'s claim.
        value_a: The value phrase extracted from ``note_a``'s claim.
        value_b: The value phrase extracted from ``note_b``'s claim.
        topic_similarity: Jaccard similarity of the two topic word sets.
        value_similarity: Jaccard similarity of the two value word sets.
        detail: Human-readable explanation (used verbatim in the report).
    """

    note_a_id: str
    note_b_id: str
    topic_a: str
    topic_b: str
    value_a: str
    value_b: str
    topic_similarity: float
    value_similarity: float
    detail: str = ""


def _extract_claims(
    notes: Iterable[Note],
) -> list[tuple[Note, str, str, set[str], set[str]]]:
    """Return the subset of ``notes`` usable as a ``(topic, value)`` claim.

    Skips deprecated notes, notes with no recognizable claim structure (see
    :func:`split_claim`), and notes whose topic or value tokenizes to an
    empty word set (nothing to compare, e.g. a value of ``"---"``). Shared by
    :func:`detect_conflicts` and :func:`analyze_conflicts` so "notes with a
    recognizable claim" means exactly the same thing in both places.
    """
    claims: list[tuple[Note, str, str, set[str], set[str]]] = []
    for note in notes:
        if note.deprecated:
            continue
        split = split_claim(note.title)
        if split is None:
            continue
        topic, value = split
        topic_words = _word_set(topic)
        value_words = _word_set(value)
        if not topic_words or not value_words:
            continue
        claims.append((note, topic, value, topic_words, value_words))
    return claims


def detect_conflicts(
    notes: Iterable[Note],
    *,
    config: dict[str, Any] | None = None,
) -> list[MemoryConflict]:
    """Flag pairs of notes that claim different values for the same topic.

    Only notes whose title splits cleanly into a ``(topic, value)`` pair (via
    :func:`split_claim`) are considered — free-form notes with no recognizable
    claim structure are skipped. Deprecated notes are skipped (already
    actioned). A pair conflicts when its topic similarity is at/above
    ``topic_similarity_threshold`` and its value similarity is *below*
    ``value_similarity_threshold``; pairs where either value has no words at
    all (an empty value after the separator) are skipped — there is no value
    to compare.

    The returned list is sorted by ``(note_a_id, note_b_id)`` so the report is
    fully deterministic regardless of the input notes' traversal order (not
    just the per-pair id ordering within each :class:`MemoryConflict`).
    """
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    topic_threshold = float(cfg["topic_similarity_threshold"])
    value_threshold = float(cfg["value_similarity_threshold"])

    claims = _extract_claims(notes)

    conflicts: list[MemoryConflict] = []
    for i, (note_a, topic_a, value_a, topic_words_a, value_words_a) in enumerate(
        claims
    ):
        for note_b, topic_b, value_b, topic_words_b, value_words_b in claims[i + 1 :]:
            topic_sim = jaccard_similarity(topic_words_a, topic_words_b)
            if topic_sim < topic_threshold:
                continue
            value_sim = jaccard_similarity(value_words_a, value_words_b)
            if value_sim >= value_threshold:
                continue

            # Canonical ordering so results are independent of input order.
            if note_a.id <= note_b.id:
                first = (note_a.id, topic_a, value_a)
                second = (note_b.id, topic_b, value_b)
            else:
                first = (note_b.id, topic_b, value_b)
                second = (note_a.id, topic_a, value_a)

            conflicts.append(
                MemoryConflict(
                    note_a_id=first[0],
                    note_b_id=second[0],
                    topic_a=first[1],
                    topic_b=second[1],
                    value_a=first[2],
                    value_b=second[2],
                    topic_similarity=topic_sim,
                    value_similarity=value_sim,
                    detail=(
                        f"Same topic ('{first[1]}' ~ '{second[1]}', "
                        f"similarity {topic_sim:.2f}) but different values: "
                        f"'{first[2]}' vs '{second[2]}' "
                        f"(value similarity {value_sim:.2f}). Both entries "
                        f"are preserved — resolve manually."
                    ),
                )
            )
    conflicts.sort(key=lambda c: (c.note_a_id, c.note_b_id))
    return conflicts


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


@dataclass
class ConflictReport:
    """Full output of :func:`analyze_conflicts`.

    Attributes:
        total_notes: Count of notes inspected.
        total_claims: Count of notes whose title split into a claim
            (topic, value) pair — the subset actually eligible for
            conflict detection.
        conflicts: All detected conflicts.
        config: The configuration used to produce the report (for audit).
    """

    total_notes: int
    total_claims: int
    conflicts: list[MemoryConflict]
    config: dict[str, Any]


def analyze_conflicts(
    notes: Iterable[Note],
    *,
    config: dict[str, Any] | None = None,
) -> ConflictReport:
    """Run conflict detection over ``notes`` and assemble a :class:`ConflictReport`.

    This is the single entry point a caller uses. It is pure: it does not
    mutate ``notes`` or touch any external state.
    """
    effective_config = {**DEFAULT_CONFIG, **(config or {})}
    materialized = list(notes)
    total = len(materialized)
    # Same eligibility rule detect_conflicts() uses, so "notes with a
    # recognizable claim" in the report matches what was actually compared.
    total_claims = len(_extract_claims(materialized))

    conflicts = detect_conflicts(materialized, config=effective_config)

    return ConflictReport(
        total_notes=total,
        total_claims=total_claims,
        conflicts=conflicts,
        config=effective_config,
    )


def render_conflict_report(report: ConflictReport) -> str:
    """Render a :class:`ConflictReport` as human-readable markdown.

    The report has two sections — summary and flagged conflicts — so a human
    (or the agent) can scan it without re-running the analysis.
    """
    lines: list[str] = []
    lines.append("# Memory Conflict Report")
    lines.append("")
    lines.append(f"- Total notes inspected: **{report.total_notes}**")
    lines.append(f"- Notes with a recognizable claim: **{report.total_claims}**")
    lines.append(f"- Conflicts detected: **{len(report.conflicts)}**")
    lines.append("")

    lines.append("## Conflicts")
    if not report.conflicts:
        lines.append("")
        lines.append("_No conflicting claims detected._")
    else:
        for conflict in report.conflicts:
            lines.append("")
            lines.append(
                f"### ⚠ CONFLICT: `{conflict.note_a_id}` vs `{conflict.note_b_id}`"
            )
            lines.append(
                f"- Topic: `{conflict.topic_a}` ~ `{conflict.topic_b}`"
                f" (similarity {conflict.topic_similarity:.2f})"
            )
            lines.append(f"- `{conflict.note_a_id}` claims: **{conflict.value_a}**")
            lines.append(f"- `{conflict.note_b_id}` claims: **{conflict.value_b}**")
            lines.append(f"- Value similarity: {conflict.value_similarity:.2f}")
            lines.append(f"- {conflict.detail}")

    lines.append("")
    return "\n".join(lines)
