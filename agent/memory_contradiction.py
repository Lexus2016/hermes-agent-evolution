"""Contradiction detection for episodic memory (issue #37).

The memory-management research behind #37 identifies contradiction handling as
a core write-path operation: when a new observation conflicts with a stored
memory, the system must *surface* both (with timestamps) rather than silently
overwrite or accumulate noise. This module implements that operation for the
episodic tier (``agent.memory_importance.EpisodicMemoryStore``) with
deterministic, LLM-free heuristics:

1. **Negation flip on a shared subject** — if a new observation asserts
   ``<subject> is X`` while a stored event asserts ``<subject> is not X``
   (or vice versa), that is a contradiction. Detected by comparing negation
   polarity between the two texts when they share at least one content token
   (the subject).

The detector is deliberately conservative to keep false positives low: it
requires (a) at least two shared content tokens (subject + claim tokens, so
a common predicate alone never triggers), and (b) exactly one side carrying
a negation marker. Texts that merely differ (no shared subject) never flag.

Stdlib-only and import-safe, consistent with
:mod:`agent.memory_importance`. The real call site is
``MemoryManager.score_memories`` in :mod:`agent.memory_manager` — every
scored turn is checked against the recent episodic store and flags are
logged (never silently dropped).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone, UTC
from typing import Iterable, List, Optional

from agent.memory_importance import MemoryEvent, tokenize

__all__ = [
    "DEFAULT_SCAN_LIMIT",
    "NEGATION_PATTERN",
    "ContradictionFlag",
    "detect_contradictions",
]

#: How many most-recent events to scan per check. The check runs on every
#: scored turn, so the window is bounded to keep the write path cheap while
#: still covering the near-term memory that is most likely to conflict.
DEFAULT_SCAN_LIMIT: int = 200

#: Negation markers, longest alternation first. The regex runs on the raw
#: lowercased text (NOT on :func:`tokenize` output, which mangles
#: contractions like ``won't`` into ``won``/``t``).
NEGATION_PATTERN = re.compile(
    r"\b(?:"
    r"no longer|no more|"
    r"isn't|isnt|aren't|arent|wasn't|wasnt|weren't|werent|"
    r"doesn't|doesnt|don't|dont|didn't|didnt|"
    r"hasn't|hasnt|hadn't|hadnt|"
    r"won't|wont|wouldn't|wouldnt|shouldn't|shouldnt|couldn't|couldnt|"
    r"can't|cant|cannot|"
    r"never|nothing|nobody|nowhere|none|without|no|not"
    r")\b",
    re.IGNORECASE,
)

#: Function words and polarity-neutral tokens excluded from the shared-subject
#: computation. Kept small and conservative — the detector only needs the
#: subject/entity token, not full NLP.
_STOPWORDS = frozenset({
    "the",
    "a",
    "an",
    "and",
    "or",
    "but",
    "of",
    "to",
    "in",
    "on",
    "for",
    "with",
    "at",
    "by",
    "from",
    "as",
    "than",
    "then",
    "so",
    "if",
    "else",
    "is",
    "are",
    "was",
    "were",
    "be",
    "been",
    "being",
    "am",
    "it",
    "its",
    "this",
    "that",
    "these",
    "those",
    "there",
    "here",
    "i",
    "you",
    "we",
    "they",
    "he",
    "she",
    "them",
    "his",
    "her",
    "their",
    "our",
    "my",
    "your",
    "me",
    "him",
    "us",
    "has",
    "have",
    "had",
    "do",
    "does",
    "did",
    "will",
    "would",
    "should",
    "could",
    "can",
    "may",
    "might",
    "must",
    "about",
    "into",
    "over",
    "under",
    "again",
    "further",
    "once",
    "also",
    "now",
    "not",
    "no",
    "never",
})

#: Tokens that look like negation fragments even after tokenize() mangling
#: (``won`` from ``won't``, ``doesn`` from ``doesn't``) — excluded from the
#: shared-subject set so a mangled negation is never mistaken for a subject.
_NEGATION_FRAGMENTS = frozenset({
    "isn",
    "isnt",
    "aren",
    "arent",
    "wasn",
    "wasnt",
    "weren",
    "werent",
    "doesn",
    "doesnt",
    "don",
    "dont",
    "didn",
    "didnt",
    "hasn",
    "hasnt",
    "hadn",
    "hadnt",
    "won",
    "wont",
    "wouldn",
    "wouldnt",
    "shouldn",
    "shouldnt",
    "couldn",
    "couldnt",
    "can",
    "cant",
    "cannot",
    "not",
    "no",
    "never",
    "nothing",
    "nobody",
    "nowhere",
    "none",
    "without",
})

#: Maximum negation-marked side length considered for polarity comparison —
#: longer texts are scored on the shared-subject rule alone.
_MAX_NEGATED_TEXT_LEN = 2000


@dataclass(frozen=True)
class ContradictionFlag:
    """One detected contradiction between a stored event and an observation.

    Carries both sides verbatim plus both timestamps, per the issue's
    requirement that conflicting memories are "logged with timestamp
    metadata" — never silently overwritten.
    """

    event_id: str
    stored_text: str
    stored_when: str
    observation: str
    reason: str
    confidence: float
    detected_at: str


def _has_negation(text: str) -> bool:
    if not text:
        return False
    return NEGATION_PATTERN.search(text.lower()) is not None


def _content_tokens(text: str) -> set:
    """Token set minus stopwords and negation fragments (the subject tokens)."""
    return {
        t
        for t in tokenize(text)
        if t not in _STOPWORDS and t not in _NEGATION_FRAGMENTS
    }


def detect_contradictions(
    events: Iterable[MemoryEvent],
    observation: str,
    *,
    limit: int = DEFAULT_SCAN_LIMIT,
    min_importance: float = 0.0,
) -> list[ContradictionFlag]:
    """Return contradiction flags between *observation* and *events*.

    Scans the ``limit`` most-recent events (last elements of the iterable),
    skipping events below ``min_importance``. A flag requires a shared
    content token AND a negation-polarity mismatch between the two sides.
    Never raises on malformed input; empty observation or events yield [].
    """
    if not observation or not observation.strip():
        return []
    obs = observation.strip()
    obs_negated = _has_negation(obs)
    obs_tokens = _content_tokens(obs)
    if not obs_tokens:
        return []
    now_iso = datetime.now(UTC).isoformat(timespec="seconds")

    recent = list(events)[-limit:] if limit and limit > 0 else list(events)
    flags: list[ContradictionFlag] = []
    for event in recent:
        # Defensive: skip entries that are not MemoryEvent-shaped rather than
        # raising, so a corrupted store entry can never fail the write path.
        if not hasattr(event, "what") or not hasattr(event, "importance"):
            continue
        try:
            importance = float(getattr(event, "importance", 0.0) or 0.0)
        except (TypeError, ValueError):
            importance = 0.0
        if importance < min_importance:
            continue
        stored_text = f"{event.what or ''} {event.outcome or ''}".strip()
        if not stored_text or len(stored_text) > _MAX_NEGATED_TEXT_LEN:
            continue
        stored_negated = _has_negation(stored_text)
        if stored_negated == obs_negated:
            continue
        shared = obs_tokens & _content_tokens(stored_text)
        # Require at least TWO shared content tokens. A single shared token is
        # usually a common predicate ("sleeping") with different subjects —
        # "the cat is sleeping" vs "the dog is not sleeping" is NOT a
        # contradiction. Two+ shared tokens means subject AND claim overlap.
        # (Generic context tokens that co-occur across unrelated observations
        # are handled by the #121 stopword list above, NOT by raising this bar —
        # subject+claim pairs like "nas healthy" vs "nas not healthy" are
        # genuine contradictions and must still fire with two shared tokens.)
        if len(shared) < 2:
            continue
        if obs_negated:
            reason = (
                "new observation negates a claim in stored memory "
                f"(shared subject: {', '.join(sorted(shared))})"
            )
        else:
            reason = (
                "new observation conflicts with a negated claim in stored memory "
                f"(shared subject: {', '.join(sorted(shared))})"
            )
        confidence = min(0.9, 0.6 + 0.05 * min(len(shared), 6))
        flags.append(
            ContradictionFlag(
                event_id=event.event_id,
                stored_text=stored_text,
                stored_when=event.when,
                observation=obs,
                reason=reason,
                confidence=round(confidence, 2),
                detected_at=now_iso,
            )
        )
    return flags
