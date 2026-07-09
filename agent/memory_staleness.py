#!/usr/bin/env python3
"""Memory staleness detection and consolidation for tqmemory notes (#797).

This module is a *side-effect-free* analysis pass over a set of memory notes.
It never mutates the notes it inspects and never calls any memory API. Instead
it produces a :class:`StalenessReport` — a structured set of recommendations
(flagged notes, consolidation groups, quality score, and a markdown rendering)
that the *caller* acts on using the existing tqmemory APIs.

Six staleness reasons are detected:

* ``AGE`` — notes older than a configurable threshold; severity scales linearly
  with age (capped at 1.0).
* ``CONTRADICTION`` — heuristic: a *newer* note containing contradiction
  language (``don't``, ``never``, ``avoid``) that shares at least one tag with
  an *older* note flags the older note.
* ``LOW_QUALITY`` — notes whose content is shorter than a configurable minimum
  length.
* ``DUPLICATE`` — near-duplicate notes detected via Jaccard similarity of their
  word sets (configurable threshold, default ``0.7``). Both members of a pair
  are flagged.
* ``SUPERSEDED`` — notes whose content contains ``supersedes: <id>`` or
  ``replacement: <id>``; the *referenced* note is flagged as superseded by the
  note containing the marker.
* ``DEPRECATED_BUT_REFERENCED`` — reserved for future use (always returns
  empty today) so callers can wire it up against a reference graph later.

The module depends only on the Python standard library, is import-safe, and is
unit-testable in isolation.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable

__all__ = [
    "Note",
    "StalenessReason",
    "StalenessFlag",
    "ConsolidationGroup",
    "StalenessReport",
    "DEFAULT_CONFIG",
    "jaccard_similarity",
    "detect_age",
    "detect_contradictions",
    "detect_low_quality",
    "detect_duplicates",
    "detect_superseded",
    "detect_deprecated_but_referenced",
    "build_consolidation_groups",
    "quality_score",
    "analyze",
    "render_report",
]


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

#: Default configuration. Values are intentionally conservative so the analysis
#: is useful out-of-the-box without tuning; every threshold is overridable via
#: :func:`analyze`'s ``config`` argument.
DEFAULT_CONFIG: dict[str, Any] = {
    # Notes older than this many days are flagged AGE.
    "max_age_days": 180,
    # Content shorter than this many characters is flagged LOW_QUALITY.
    "min_content_length": 20,
    # Jaccard word-set similarity at/above this fraction flags DUPLICATE.
    "duplicate_jaccard_threshold": 0.7,
    # Tag overlap required for a contradiction relationship between two notes.
    "contradiction_min_tag_overlap": 1,
    # Contradiction cue words (lowercase, matched as whole words).
    "contradiction_cues": ("don't", "never", "avoid"),
}


# ---------------------------------------------------------------------------
# Note model
# ---------------------------------------------------------------------------


@dataclass
class Note:
    """Minimal representation of a tqmemory note.

    Attributes:
        id: Stable identifier (as used by the tqmemory store).
        title: Human-readable title.
        content: Free-form body text.
        kind: Note kind/category (e.g. ``"note"``, ``"preference"``).
        tags: List of tags (order preserved, duplicates ignored for
            similarity comparisons).
        created_at: Creation timestamp (timezone-aware recommended).
        updated_at: Last-modification timestamp.
        deprecated: Whether the note is already marked deprecated upstream.
    """

    id: str
    title: str
    content: str
    kind: str = "note"
    tags: list[str] = field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None
    deprecated: bool = False

    def age_days(self, *, now: datetime | None = None) -> float:
        """Return the note's age in (fractional) days, relative to ``now``.

        Age is measured from :attr:`updated_at` when present (a note that was
        recently refreshed should not look stale just because it was *created*
        long ago), falling back to :attr:`created_at`. If neither timestamp is
        set the note is treated as age 0.0 — unknown age is not itself a
        staleness signal. ``now`` defaults to ``datetime.now(timezone.utc)``.
        """
        ref = self.updated_at or self.created_at
        if ref is None:
            return 0.0
        anchor = now or datetime.now(timezone.utc)
        # Normalize naive timestamps to UTC so the delta is always well-defined.
        if ref.tzinfo is None:
            ref = ref.replace(tzinfo=timezone.utc)
        if anchor.tzinfo is None:
            anchor = anchor.replace(tzinfo=timezone.utc)
        delta = anchor - ref
        return max(delta.total_seconds(), 0.0) / 86400.0

    def word_set(self) -> set[str]:
        """Return the normalized set of words used for similarity comparison.

        The title and content are concatenated, lowercased, and split on
        non-alphanumeric runs. This deliberately does no stemming — it is a
        cheap, deterministic tokenization whose only job is to surface
        near-duplicates via Jaccard similarity, not to model semantics.
        """
        text = f"{self.title} {self.content}".lower()
        return {w for w in _WORD_RE.split(text) if w}


# Module-level compiled regex. Kept outside the class body so dataclasses don't
# treat it as a field; mirrored onto the class via ClassVar for doc-readability.
_WORD_RE = re.compile(r"[^a-z0-9]+")


# ---------------------------------------------------------------------------
# Staleness flags
# ---------------------------------------------------------------------------


class StalenessReason(str, Enum):
    """The six reasons a note may be flagged as stale."""

    AGE = "AGE"
    CONTRADICTION = "CONTRADICTION"
    LOW_QUALITY = "LOW_QUALITY"
    DUPLICATE = "DUPLICATE"
    SUPERSEDED = "SUPERSEDED"
    DEPRECATED_BUT_REFERENCED = "DEPRECATED_BUT_REFERENCED"


@dataclass
class StalenessFlag:
    """A single staleness finding against one note.

    Attributes:
        note_id: The flagged note's id.
        reason: Why it was flagged.
        severity: 0.0–1.0; 1.0 means maximally severe.
        detail: Human-readable explanation (used verbatim in the report).
        related_ids: Other note ids implicated in the finding (e.g. the newer
            contradicting note, or the duplicate partner).
    """

    note_id: str
    reason: StalenessReason
    severity: float = 1.0
    detail: str = ""
    related_ids: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Detectors (pure functions)
# ---------------------------------------------------------------------------


def jaccard_similarity(a: set[str], b: set[str]) -> float:
    """Jaccard similarity ``|A ∩ B| / |A ∪ B|`` of two word sets.

    Returns ``0.0`` when both sets are empty (no words to compare) rather than
    the mathematically undefined ``0/0`` — two empty notes are not considered
    duplicates of each other.
    """
    if not a and not b:
        return 0.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def detect_age(
    notes: Iterable[Note],
    *,
    config: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> list[StalenessFlag]:
    """Flag notes older than ``max_age_days``.

    Severity scales *linearly* with age: a note exactly at the threshold has
    severity ``0.0`` (just barely stale), doubling to ``1.0`` at twice the
    threshold, capped at ``1.0``. Notes already deprecated upstream are skipped
    — they are stale by definition and should not crowd the AGE bucket.
    """
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    max_age = float(cfg["max_age_days"])
    if max_age <= 0:
        return []
    flags: list[StalenessFlag] = []
    for n in notes:
        if n.deprecated:
            continue
        age = n.age_days(now=now)
        if age >= max_age:
            severity = min((age - max_age) / max_age, 1.0) if max_age else 0.0
            flags.append(
                StalenessFlag(
                    note_id=n.id,
                    reason=StalenessReason.AGE,
                    severity=severity,
                    detail=f"Note is {age:.1f} days old (threshold {max_age:.0f} days).",
                )
            )
    return flags


def detect_low_quality(
    notes: Iterable[Note],
    *,
    config: dict[str, Any] | None = None,
) -> list[StalenessFlag]:
    """Flag notes whose content is shorter than ``min_content_length`` chars.

    Deprecated notes are skipped (already actioned). Severity is fixed at 1.0 —
    a note below the minimum length threshold is a clear quality signal.
    """
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    min_len = int(cfg["min_content_length"])
    flags: list[StalenessFlag] = []
    for n in notes:
        if n.deprecated:
            continue
        content_len = len(n.content or "")
        if content_len < min_len:
            flags.append(
                StalenessFlag(
                    note_id=n.id,
                    reason=StalenessReason.LOW_QUALITY,
                    severity=1.0,
                    detail=(
                        f"Content is {content_len} chars, below the "
                        f"{min_len}-char minimum."
                    ),
                )
            )
    return flags


def detect_contradictions(
    notes: Iterable[Note],
    *,
    config: dict[str, Any] | None = None,
) -> list[StalenessFlag]:
    """Heuristic contradiction detection.

    A *newer* note containing a contradiction cue word (``don't``,
    ``never``, ``avoid`` by default) that shares at least one tag with an
    *older* note flags the older note. The newer note is the source of truth,
    so the older one is the stale artifact. Each (newer, older) pair produces one
    flag against the older note; the newer note's id is recorded in
    ``related_ids`` so the report can link them.

    Notes without timestamps, deprecated notes, and pairs with insufficient tag
    overlap are skipped. Only the older note is flagged (the newer note is the
    correction, not the stale item).
    """
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    cues = tuple(c.lower() for c in cfg["contradiction_cues"])
    min_overlap = int(cfg["contradiction_min_tag_overlap"])
    # Precompile a word-boundary regex per cue for robust whole-word matching.
    cue_re = re.compile(
        r"\b(" + "|".join(re.escape(c) for c in cues) + r")\b",
        re.IGNORECASE,
    )

    materialized = [n for n in notes if n.created_at is not None and not n.deprecated]
    # Sort oldest-first so "newer" is any note that comes later in the list and
    # has a strictly greater created_at.
    materialized.sort(key=lambda n: n.created_at)  # type: ignore[arg-type]

    tags_by_id = {n.id: set(t.lower() for t in n.tags) for n in materialized}
    flags: list[StalenessFlag] = []
    for i, newer in enumerate(materialized):
        if not cue_re.search(newer.content or ""):
            continue
        newer_tags = tags_by_id[newer.id]
        for older in materialized[:i]:
            # Strictly newer (created_at), with at least min_overlap shared tags.
            if older.created_at >= newer.created_at:  # type: ignore[operator]
                continue
            older_tags = tags_by_id[older.id]
            if len(newer_tags & older_tags) < min_overlap:
                continue
            flags.append(
                StalenessFlag(
                    note_id=older.id,
                    reason=StalenessReason.CONTRADICTION,
                    severity=0.8,
                    detail=(
                        f"Newer note '{newer.id}' uses contradiction language "
                        f"and shares tags with this older note."
                    ),
                    related_ids=[newer.id],
                )
            )
    return flags


def detect_duplicates(
    notes: Iterable[Note],
    *,
    config: dict[str, Any] | None = None,
) -> list[StalenessFlag]:
    """Flag near-duplicate notes via Jaccard word-set similarity.

    Every pair of non-deprecated notes whose Jaccard similarity is at/above
    ``duplicate_jaccard_threshold`` (default ``0.7``) produces a flag against
    *each* member of the pair, with the partner recorded in ``related_ids``.
    Severity is the similarity itself (a 0.7 match → severity 0.7).
    """
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    threshold = float(cfg["duplicate_jaccard_threshold"])
    materialized = [n for n in notes if not n.deprecated]
    word_sets = {n.id: n.word_set() for n in materialized}
    flags: list[StalenessFlag] = []
    for i, a in enumerate(materialized):
        wa = word_sets[a.id]
        for b in materialized[i + 1 :]:
            wb = word_sets[b.id]
            sim = jaccard_similarity(wa, wb)
            if sim >= threshold:
                flags.append(
                    StalenessFlag(
                        note_id=a.id,
                        reason=StalenessReason.DUPLICATE,
                        severity=sim,
                        detail=(
                            f"Near-duplicate of '{b.id}' "
                            f"(Jaccard {sim:.2f} ≥ {threshold:.2f})."
                        ),
                        related_ids=[b.id],
                    )
                )
                flags.append(
                    StalenessFlag(
                        note_id=b.id,
                        reason=StalenessReason.DUPLICATE,
                        severity=sim,
                        detail=(
                            f"Near-duplicate of '{a.id}' "
                            f"(Jaccard {sim:.2f} ≥ {threshold:.2f})."
                        ),
                        related_ids=[a.id],
                    )
                )
    return flags


# Matches "supersedes: <id>" or "replacement: <id>" (case-insensitive, any
# whitespace around the colon, id is a non-whitespace token).
_SUPERSEDE_RE = re.compile(
    r"\b(?:supersedes|replacement)\s*:\s*([^\s,;]+)", re.IGNORECASE
)


def detect_superseded(
    notes: Iterable[Note],
    *,
    config: dict[str, Any] | None = None,
) -> list[StalenessFlag]:
    """Flag notes explicitly superseded via ``supersedes: <id>`` markers.

    A note whose content contains ``supersedes: <id>`` or ``replacement: <id>``
    declares that it replaces the note with the given id. The *referenced* note
    is flagged as superseded; the declaring note is the canonical replacement
    and is recorded in ``related_ids``. Unknown ids (no matching note in the
    input set) are ignored — the analysis only operates on notes it can see.
    """
    del config  # configuration-free; kept for API symmetry.
    materialized = list(notes)
    ids = {n.id for n in materialized}
    flags: list[StalenessFlag] = []
    for declarer in materialized:
        if declarer.deprecated:
            continue
        for match in _SUPERSEDE_RE.finditer(declarer.content or ""):
            ref_id = match.group(1)
            if ref_id == declarer.id:
                continue  # a note cannot supersede itself
            if ref_id not in ids:
                continue  # references a note we can't see; ignore
            target = next(n for n in materialized if n.id == ref_id)
            if target.deprecated:
                continue  # already actioned
            flags.append(
                StalenessFlag(
                    note_id=ref_id,
                    reason=StalenessReason.SUPERSEDED,
                    severity=1.0,
                    detail=(
                        f"Superseded by '{declarer.id}' "
                        f"via '{match.group(0).strip()}' marker."
                    ),
                    related_ids=[declarer.id],
                )
            )
    return flags


def detect_deprecated_but_referenced(
    notes: Iterable[Note],
    *,
    config: dict[str, Any] | None = None,
) -> list[StalenessFlag]:
    """Reserved for future use — always returns an empty list today.

    A full implementation would build a reference graph over note ids and flag
    any *deprecated* note that is still referenced by a *live* note's content.
    That requires a reference-extraction pass that is out of scope for #797; the
    hook exists now so callers can wire it up without changing the report shape.
    """
    del notes, config  # no references consumed yet; signature is stable.
    return []


# ---------------------------------------------------------------------------
# Consolidation groups
# ---------------------------------------------------------------------------


@dataclass
class ConsolidationGroup:
    """A suggestion to merge a set of near-duplicate notes into one.

    Attributes:
        note_ids: All note ids in the cluster (including the canonical one).
        canonical_id: The id of the note to keep; the rest should be deprecated.
        reason: Why these notes were grouped (always ``DUPLICATE`` today).
    """

    note_ids: list[str]
    canonical_id: str
    reason: str = "DUPLICATE"


def build_consolidation_groups(
    notes: Iterable[Note],
    *,
    config: dict[str, Any] | None = None,
) -> list[ConsolidationGroup]:
    """Cluster near-duplicate notes and pick a canonical (newest) member.

    Greedy single-linkage clustering over the duplicate graph: two notes are
    linked when their Jaccard similarity is at/above
    ``duplicate_jaccard_threshold``. The canonical note in each cluster is the
    *newest* by ``updated_at`` (falling back to ``created_at``, then to the
    lexicographically largest id as a deterministic tiebreaker). Deprecated
    notes are excluded from clustering — they are already on their way out.
    """
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    threshold = float(cfg["duplicate_jaccard_threshold"])
    materialized = [n for n in notes if not n.deprecated]
    word_sets = {n.id: n.word_set() for n in materialized}

    # Union-find over note ids.
    parent: dict[str, str] = {n.id: n.id for n in materialized}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for i, a in enumerate(materialized):
        wa = word_sets[a.id]
        for b in materialized[i + 1 :]:
            if jaccard_similarity(wa, word_sets[b.id]) >= threshold:
                union(a.id, b.id)

    # Group by root.
    clusters: dict[str, list[Note]] = {}
    for n in materialized:
        clusters.setdefault(find(n.id), []).append(n)

    groups: list[ConsolidationGroup] = []
    for members in clusters.values():
        if len(members) < 2:
            continue  # no duplicates to consolidate

        def sort_key(n: Note) -> tuple[Any, ...]:
            updated = n.updated_at or datetime.min.replace(tzinfo=timezone.utc)
            created = n.created_at or datetime.min.replace(tzinfo=timezone.utc)
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            # Newest first: highest timestamp wins; id breaks ties deterministically.
            return (updated, created, n.id)

        canonical = sorted(members, key=sort_key, reverse=True)[0]
        ids = [n.id for n in members]
        # Canonical first for readability, then the rest.
        ids_sorted = [canonical.id] + [i for i in ids if i != canonical.id]
        groups.append(
            ConsolidationGroup(
                note_ids=ids_sorted,
                canonical_id=canonical.id,
                reason="DUPLICATE",
            )
        )
    return groups


# ---------------------------------------------------------------------------
# Quality score
# ---------------------------------------------------------------------------


def quality_score(
    total_notes: int,
    flagged_note_ids: Iterable[str],
) -> float:
    """Non-linear quality score in ``[0.0, 1.0]``.

    ``1.0`` means every note is pristine (none flagged); ``0.0`` means every
    note is flagged. The metric is *non-linear* so that a small fraction of
    flagged notes does not crater the score: it uses ``1 - (f ** 2)``, which
    is gentler than the linear ``1 - f`` for small ``f`` but still reaches 0
    when every note is flagged. With zero notes the corpus is trivially
    pristine (``1.0``).
    """
    if total_notes <= 0:
        return 1.0
    unique_flagged = len(set(flagged_note_ids))
    fraction = unique_flagged / total_notes
    fraction = min(max(fraction, 0.0), 1.0)
    return 1.0 - (fraction**2)


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


@dataclass
class StalenessReport:
    """Full output of :func:`analyze`.

    Attributes:
        total_notes: Count of notes inspected.
        flags: All staleness flags across all detectors.
        consolidation_groups: Merge suggestions built from duplicates.
        quality_score: Corpus quality in ``[0.0, 1.0]``.
        config: The configuration used to produce the report (for audit).
    """

    total_notes: int
    flags: list[StalenessFlag]
    consolidation_groups: list[ConsolidationGroup]
    quality_score: float
    config: dict[str, Any]


def analyze(
    notes: Iterable[Note],
    *,
    config: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> StalenessReport:
    """Run all staleness detectors and assemble a :class:`StalenessReport`.

    This is the single entry point a caller uses. It is pure: it does not
    mutate ``notes`` or touch any external state. Pass ``now`` to make the
    age detector deterministic in tests.
    """
    effective_config = {**DEFAULT_CONFIG, **(config or {})}
    materialized = list(notes)
    total = len(materialized)

    all_flags: list[StalenessFlag] = []
    all_flags.extend(detect_age(materialized, config=effective_config, now=now))
    all_flags.extend(detect_low_quality(materialized, config=effective_config))
    all_flags.extend(detect_contradictions(materialized, config=effective_config))
    all_flags.extend(detect_duplicates(materialized, config=effective_config))
    all_flags.extend(detect_superseded(materialized, config=effective_config))
    all_flags.extend(
        detect_deprecated_but_referenced(materialized, config=effective_config)
    )

    groups = build_consolidation_groups(materialized, config=effective_config)
    flagged_ids = {f.note_id for f in all_flags}
    score = quality_score(total, flagged_ids)

    return StalenessReport(
        total_notes=total,
        flags=all_flags,
        consolidation_groups=groups,
        quality_score=score,
        config=effective_config,
    )


def render_report(report: StalenessReport) -> str:
    """Render a :class:`StalenessReport` as human-readable markdown.

    The report has four sections — summary, flagged notes (grouped by reason),
    consolidation suggestions, and quality metrics — so a human can scan it
    without re-running the analysis.
    """
    lines: list[str] = []
    lines.append("# Memory Staleness Report")
    lines.append("")
    lines.append(f"- Total notes inspected: **{report.total_notes}**")
    lines.append(f"- Staleness flags raised: **{len(report.flags)}**")
    flagged_ids = {f.note_id for f in report.flags}
    lines.append(f"- Notes flagged (unique): **{len(flagged_ids)}**")
    lines.append(f"- Consolidation groups: **{len(report.consolidation_groups)}**")
    lines.append(f"- Quality score: **{report.quality_score:.2f}** / 1.00")
    lines.append("")

    # Group flags by reason for readability.
    by_reason: dict[StalenessReason, list[StalenessFlag]] = {}
    for flag in report.flags:
        by_reason.setdefault(flag.reason, []).append(flag)

    lines.append("## Flagged Notes")
    if not report.flags:
        lines.append("")
        lines.append("_No staleness flags detected. The memory corpus looks healthy._")
    else:
        for reason in StalenessReason:
            group = by_reason.get(reason, [])
            if not group:
                continue
            lines.append("")
            lines.append(f"### {reason.value} ({len(group)})")
            for flag in group:
                related = ""
                if flag.related_ids:
                    joined = ", ".join(f"`{rid}`" for rid in flag.related_ids)
                    related = f" — related: {joined}"
                lines.append(
                    f"- `{flag.note_id}` (severity {flag.severity:.2f}): "
                    f"{flag.detail}{related}"
                )

    lines.append("")
    lines.append("## Consolidation Suggestions")
    if not report.consolidation_groups:
        lines.append("")
        lines.append("_No consolidation groups identified._")
    else:
        for i, group in enumerate(report.consolidation_groups, start=1):
            members = ", ".join(
                f"`{nid}`" + (" (canonical)" if nid == group.canonical_id else "")
                for nid in group.note_ids
            )
            lines.append(f"{i}. **{group.reason}**: {members}")

    lines.append("")
    lines.append("## Quality Metrics")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    lines.append(f"| Total notes | {report.total_notes} |")
    lines.append(f"| Flagged notes | {len(flagged_ids)} |")
    pct = (len(flagged_ids) / report.total_notes * 100.0) if report.total_notes else 0.0
    lines.append(f"| Flagged fraction | {pct:.1f}% |")
    lines.append(f"| Quality score | {report.quality_score:.2f} |")
    lines.append("")

    return "\n".join(lines)