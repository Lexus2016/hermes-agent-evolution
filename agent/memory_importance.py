"""Memory importance scoring and episodic retrieval (#752).

A self-contained module (stdlib only) that:

1. Defines a ``MemoryEvent`` schema with full JSON round-trip
   (``to_dict`` / ``from_dict``).
2. Scores event importance from friction signals using a weighted model,
   with exponential temporal decay over a configurable half-life.
3. Provides an ``EpisodicMemoryStore`` that stores events and retrieves
   ordered sequences by time range, category/tags, importance threshold,
   text search (bag-of-words TF-IDF-like scoring), and temporal
   proximity to a reference timestamp.
4. Deduplicates events via Jaccard similarity on tokenized text, merging
   groups while keeping the highest-importance event and combining tags.
5. Persists the whole store to / loads it from a JSON file.

Import-safe: importing this module has no side effects and requires no
third-party packages.  Designed to be unit-testable with only stdlib +
pytest + unittest.mock.
"""

from __future__ import annotations

import json
import math
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

__all__ = [
    "MemoryEvent",
    "EpisodicMemoryStore",
    "score_importance",
    "apply_temporal_decay",
    "tokenize",
    "jaccard_similarity",
    "DEFAULT_HALF_LIFE_DAYS",
    "DEFAULT_SIGNAL_WEIGHTS",
    "SIGNAL_WEIGHTS",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Default half-life for exponential temporal decay, in days.
DEFAULT_HALF_LIFE_DAYS: float = 30.0

#: Canonical weighted friction-signal model.  The keys are the friction
#: signal names; the values are the raw (pre-decay) weights.  They sum to
#: exactly 1.0 so a maximally-frictioned event with no decay scores 1.0.
DEFAULT_SIGNAL_WEIGHTS: dict[str, float] = {
    "retries": 0.30,
    "human_corrections": 0.25,
    "task_failures": 0.20,
    "explicit_saves": 0.15,
    "novelty_recency": 0.10,
}

#: Public alias matching the task spec wording.
SIGNAL_WEIGHTS: dict[str, float] = DEFAULT_SIGNAL_WEIGHTS

_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(ts: str | datetime | None) -> datetime:
    """Parse an ISO timestamp (or pass through a datetime) to a UTC datetime.

    Accepts naive datetimes (assumed UTC) and offsets; trailing ``Z`` is
    normalized.  ``None`` defaults to *now*.
    """
    if ts is None:
        return datetime.now(timezone.utc)
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            return ts.replace(tzinfo=timezone.utc)
        return ts
    cleaned = ts.strip()
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    dt = datetime.fromisoformat(cleaned)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def tokenize(text: str) -> set[str]:
    """Tokenize free text into a lowercase alphanumeric token set.

    Simple but deterministic: splits on non ``[A-Za-z0-9_]`` runs and
    lowercases.  Returns a *set* so it is directly usable for Jaccard
    similarity and bag-of-words overlap.
    """
    if not text:
        return set()
    return {tok.lower() for tok in _TOKEN_RE.findall(text)}


def jaccard_similarity(a: Iterable[str], b: Iterable[str]) -> float:
    """Jaccard similarity (intersection / union) of two token iterables.

    Returns 0.0 when both sets are empty (by convention two empty texts
    are *not* considered identical), and 1.0 when the union is non-empty
    and fully overlapping.
    """
    sa, sb = set(a), set(b)
    union = sa | sb
    if not union:
        return 0.0
    return len(sa & sb) / len(union)


# ---------------------------------------------------------------------------
# Importance scoring
# ---------------------------------------------------------------------------


def _normalize_signal(name: str) -> str:
    """Map common spellings of friction-signal names to canonical keys."""
    aliases: dict[str, str] = {
        "retry": "retries",
        "retries": "retries",
        "human_correction": "human_corrections",
        "human_corrections": "human_corrections",
        "correction": "human_corrections",
        "corrections": "human_corrections",
        "task_failure": "task_failures",
        "task_failures": "task_failures",
        "failure": "task_failures",
        "failures": "task_failures",
        "explicit_save": "explicit_saves",
        "explicit_saves": "explicit_saves",
        "save": "explicit_saves",
        "saves": "explicit_saves",
        "novelty_recency": "novelty_recency",
        "novelty": "novelty_recency",
        "recency": "novelty_recency",
    }
    return aliases.get(name, name)


def score_importance(
    friction_signals: dict[str, int | float],
    *,
    weights: dict[str, float] | None = None,
    signal_scale: float = 3.0,
) -> float:
    """Compute a raw (pre-decay) importance score in ``[0.0, 1.0]``.

    Each friction signal contributes ``weight * (1 - exp(-count / scale))``
    so a count of zero contributes nothing, and higher counts saturate
    asymptotically toward the signal's full weight.  ``signal_scale``
    controls how quickly a signal saturates (default 3.0 ≈ a count of 3
    captures ~63% of the weight).

    Unknown signal keys are ignored (they are not part of the canonical
    weighted model) but do not raise.
    """
    w = weights if weights is not None else DEFAULT_SIGNAL_WEIGHTS
    total = 0.0
    for raw_name, count in friction_signals.items():
        name = _normalize_signal(raw_name)
        weight = w.get(name)
        if weight is None or not weight:
            continue
        c = float(count)
        if c <= 0:
            continue
        total += weight * (1.0 - math.exp(-c / signal_scale))
    return max(0.0, min(1.0, total))


def apply_temporal_decay(
    score: float,
    event_time: str | datetime,
    *,
    reference_time: str | datetime | None = None,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
) -> float:
    """Apply exponential temporal decay to a raw importance score.

    ``decayed = score * 0.5 ** (elapsed_days / half_life)``.  Events in the
    future relative to ``reference_time`` are treated as elapsed=0 (no
    boost).  ``half_life_days`` must be positive.
    """
    if half_life_days <= 0:
        raise ValueError("half_life_days must be positive")
    ref = (
        _parse_iso(reference_time)
        if reference_time is not None
        else datetime.now(timezone.utc)
    )
    ev = _parse_iso(event_time)
    elapsed = (ref - ev).total_seconds() / 86400.0
    if elapsed < 0:
        elapsed = 0.0
    decay = 0.5 ** (elapsed / half_life_days)
    return max(0.0, min(1.0, score * decay))


# ---------------------------------------------------------------------------
# MemoryEvent schema
# ---------------------------------------------------------------------------


def _new_event_id() -> str:
    return uuid.uuid4().hex


@dataclass
class MemoryEvent:
    """A single episodic memory event.

    Attributes mirror the task spec.  ``importance`` is the *raw* (pre-decay)
    score; callers can compute a decayed score on demand via the store.  All
    fields have sensible defaults so the minimum viable event is just a
    ``what`` and a ``when``.
    """

    what: str
    when: str = field(default_factory=_now_iso)
    event_id: str = field(default_factory=_new_event_id)
    outcome: str = ""
    importance: float = 0.0
    friction_signals: dict[str, int] = field(default_factory=dict)
    category: str = ""
    tags: list[str] = field(default_factory=list)
    context_refs: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Coerce common alternate types so the dataclass is forgiving.
        # ``None`` for a container field falls back to the empty default
        # rather than raising (dict(None)/list(None) both TypeError).
        if not isinstance(self.importance, (int, float)) or self.importance is None:
            self.importance = float(self.importance or 0.0)
        self.importance = max(0.0, min(1.0, float(self.importance)))
        self.friction_signals = (
            dict(self.friction_signals) if self.friction_signals else {}
        )
        self.tags = list(self.tags) if self.tags else []
        self.context_refs = list(self.context_refs) if self.context_refs else []
        self.metadata = dict(self.metadata) if self.metadata else {}

    # -- scoring -----------------------------------------------------------

    def raw_importance(
        self,
        *,
        weights: dict[str, float] | None = None,
        signal_scale: float = 3.0,
    ) -> float:
        """Recompute the raw importance from this event's friction signals."""
        return score_importance(
            self.friction_signals, weights=weights, signal_scale=signal_scale
        )

    def decayed_importance(
        self,
        *,
        reference_time: str | datetime | None = None,
        half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
        weights: dict[str, float] | None = None,
        signal_scale: float = 3.0,
        use_raw: bool = False,
    ) -> float:
        """Return the time-decayed importance.

        By default the raw score is recomputed from ``friction_signals`` so
        callers always get a value consistent with the current weights.  Pass
        ``use_raw=True`` to decay the stored ``importance`` field instead.
        """
        base = (
            self.importance
            if use_raw
            else self.raw_importance(weights=weights, signal_scale=signal_scale)
        )
        return apply_temporal_decay(
            base,
            self.when,
            reference_time=reference_time,
            half_life_days=half_life_days,
        )

    # -- serialization ----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-friendly dict (lossless round-trip)."""
        return {
            "event_id": self.event_id,
            "what": self.what,
            "when": self.when,
            "outcome": self.outcome,
            "importance": self.importance,
            "friction_signals": dict(self.friction_signals),
            "category": self.category,
            "tags": list(self.tags),
            "context_refs": list(self.context_refs),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MemoryEvent:
        """Reconstruct a ``MemoryEvent`` from a serialized dict.

        Missing keys fall back to dataclass defaults; unknown keys are
        ignored so older/newer serializations remain compatible.
        """
        known = {
            "event_id",
            "what",
            "when",
            "outcome",
            "importance",
            "friction_signals",
            "category",
            "tags",
            "context_refs",
            "metadata",
        }
        kwargs = {k: v for k, v in data.items() if k in known}
        return cls(**kwargs)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> MemoryEvent:
        return cls.from_dict(json.loads(text))

    def tokens(self) -> set[str]:
        """Token set over ``what`` + ``outcome`` + ``tags`` for search/dedup."""
        blob = " ".join([self.what, self.outcome, *self.tags])
        return tokenize(blob)


# ---------------------------------------------------------------------------
# EpisodicMemoryStore
# ---------------------------------------------------------------------------


@dataclass
class EpisodicMemoryStore:
    """In-memory episodic memory store with retrieval, dedup, and persistence.

    The store keeps events in insertion order in ``self.events`` (a dict
    keyed by ``event_id``) so retrieval results are deterministic.  All
    retrieval methods return *new* lists (never the internal views).
    """

    events: dict[str, MemoryEvent] = field(default_factory=dict)
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS

    # -- mutation ---------------------------------------------------------

    def add(self, event: MemoryEvent) -> MemoryEvent:
        """Insert (or replace, if the id collides) an event."""
        self.events[event.event_id] = event
        return event

    def add_many(self, events: Iterable[MemoryEvent]) -> list[MemoryEvent]:
        return [self.add(e) for e in events]

    def remove(self, event_id: str) -> bool:
        return self.events.pop(event_id, None) is not None

    def get(self, event_id: str) -> MemoryEvent | None:
        return self.events.get(event_id)

    def __len__(self) -> int:
        return len(self.events)

    def __contains__(self, event_id: object) -> bool:
        return event_id in self.events

    def all(self) -> list[MemoryEvent]:
        """Return all events in insertion order."""
        return list(self.events.values())

    # -- retrieval ---------------------------------------------------------

    def _ordered(
        self, events: Iterable[MemoryEvent], *, by: str = "when"
    ) -> list[MemoryEvent]:
        """Return a list ordered by ``when`` (default) ascending."""
        items = list(events)
        if by == "importance":
            items.sort(key=lambda e: e.importance, reverse=True)
        else:
            items.sort(key=lambda e: _parse_iso(e.when))
        return items

    def retrieve_by_time_range(
        self,
        start: str | datetime | None = None,
        end: str | datetime | None = None,
        *,
        inclusive: bool = True,
    ) -> list[MemoryEvent]:
        """Return events with ``when`` in ``[start, end]`` (open-ended if None)."""
        s = _parse_iso(start) if start is not None else None
        e = _parse_iso(end) if end is not None else None
        out: list[MemoryEvent] = []
        for ev in self.events.values():
            t = _parse_iso(ev.when)
            if s is not None:
                if inclusive and t < s:
                    continue
                if not inclusive and t <= s:
                    continue
            if e is not None:
                if inclusive and t > e:
                    continue
                if not inclusive and t >= e:
                    continue
            out.append(ev)
        return self._ordered(out)

    def retrieve_by_category(
        self, category: str | None = None, *, tags: Iterable[str] | None = None
    ) -> list[MemoryEvent]:
        """Return events matching a category and/or any of the given tags."""
        tag_set = set(tags) if tags is not None else None
        out: list[MemoryEvent] = []
        for ev in self.events.values():
            if category is not None and ev.category != category:
                continue
            if tag_set is not None:
                if not (set(ev.tags) & tag_set):
                    continue
            out.append(ev)
        return self._ordered(out)

    def retrieve_by_importance(
        self,
        threshold: float = 0.0,
        *,
        decayed: bool = False,
        reference_time: str | datetime | None = None,
    ) -> list[MemoryEvent]:
        """Return events with importance >= ``threshold`` (descending)."""
        out: list[MemoryEvent] = []
        for ev in self.events.values():
            score = (
                ev.decayed_importance(
                    reference_time=reference_time,
                    half_life_days=self.half_life_days,
                    use_raw=True,
                )
                if decayed
                else ev.importance
            )
            if score >= threshold:
                out.append(ev)
        out.sort(key=lambda e: e.importance, reverse=True)
        return out

    def text_search(self, query: str, *, min_score: float = 0.0) -> list[MemoryEvent]:
        """Bag-of-words TF-IDF-like search over event text.

        Each event's token bag is built from ``what`` + ``outcome`` + ``tags``.
        IDF is computed over the store corpus; the query is scored by summed
        ``tf * idf`` over query tokens present in each event, normalized by
        event token count.  Results are returned in descending score order.
        """
        if not query.strip():
            return []
        q_tokens = tokenize(query)
        if not q_tokens:
            return []

        corpus = [ev.tokens() for ev in self.events.values()]
        n_docs = len(corpus)
        # document frequency per token
        df: dict[str, int] = {}
        for toks in corpus:
            for tok in toks:
                df[tok] = df.get(tok, 0) + 1
        if n_docs == 0:
            return []

        scored: list[tuple[float, MemoryEvent]] = []
        for ev, toks in zip(self.events.values(), corpus):
            if not toks:
                continue
            tf: dict[str, int] = {}
            # recompute term frequency for this event deterministically
            for tok in toks:
                tf[tok] = tf.get(tok, 0) + 1
            denom = len(toks)
            score = 0.0
            for tok in q_tokens:
                if tok not in tf:
                    continue
                idf = math.log((1 + n_docs) / (1 + df.get(tok, 0))) + 1.0
                score += (tf[tok] / denom) * idf
            if score >= min_score:
                scored.append((score, ev))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [ev for _, ev in scored]

    def retrieve_by_temporal_proximity(
        self,
        reference: str | datetime,
        *,
        limit: int = 10,
        max_window_days: float | None = None,
    ) -> list[MemoryEvent]:
        """Return the ``limit`` events closest in time to ``reference``.

        Ordering is by absolute temporal distance, ascending.  If
        ``max_window_days`` is given, events farther than that are excluded.
        """
        ref = _parse_iso(reference)
        scored: list[tuple[float, MemoryEvent]] = []
        for ev in self.events.values():
            delta = abs((_parse_iso(ev.when) - ref).total_seconds()) / 86400.0
            if max_window_days is not None and delta > max_window_days:
                continue
            scored.append((delta, ev))
        scored.sort(key=lambda x: (x[0], x[1].event_id))
        return [ev for _, ev in scored[:limit]]

    # -- deduplication ----------------------------------------------------

    def deduplicate(
        self, *, threshold: float = 0.8, inplace: bool = True
    ) -> list[MemoryEvent]:
        """Group near-duplicate events by Jaccard similarity and merge.

        Two events are considered duplicates when
        ``jaccard_similarity(a.tokens(), b.tokens()) >= threshold``.  Within
        each group the event with the highest ``importance`` is kept as the
        representative; tags from all group members are unioned (preserving
        first-seen order); friction signals are summed; ``context_refs`` are
        unioned; the kept event's ``when`` is the earliest in the group (so
        the representative anchors the episode's start).

        Returns the resulting list of (possibly merged) events.  When
        ``inplace`` is True the store's events are replaced by the merged
        set; when False the store is untouched and only the merged list is
        returned.
        """
        events = list(self.events.values())
        # Deterministic grouping: sort by when so earlier events seed groups.
        events.sort(key=lambda e: (_parse_iso(e.when), e.event_id))
        groups: list[list[MemoryEvent]] = []
        for ev in events:
            placed = False
            for group in groups:
                rep = group[0]
                if jaccard_similarity(rep.tokens(), ev.tokens()) >= threshold:
                    group.append(ev)
                    placed = True
                    break
            if not placed:
                groups.append([ev])

        merged: list[MemoryEvent] = []
        for group in groups:
            merged.append(self._merge_group(group))

        if inplace:
            new_events: dict[str, MemoryEvent] = {}
            for ev in merged:
                new_events[ev.event_id] = ev
            self.events = new_events
        return merged

    @staticmethod
    def _merge_group(group: list[MemoryEvent]) -> MemoryEvent:
        """Merge a duplicate group into one representative event."""
        # Highest importance wins; ties broken by earliest when then event_id.
        rep = max(
            group,
            key=lambda e: (e.importance, -_parse_iso(e.when).timestamp(), e.event_id),
        )
        # Earliest timestamp anchors the episode.
        earliest = min(group, key=lambda e: (_parse_iso(e.when), e.event_id))
        # Union tags preserving first-seen order.
        tags: list[str] = []
        seen: set[str] = set()
        for ev in sorted(group, key=lambda e: (_parse_iso(e.when), e.event_id)):
            for t in ev.tags:
                if t not in seen:
                    seen.add(t)
                    tags.append(t)
        # Union context refs (first-seen order).
        ctx: list[str] = []
        seen_ctx: set[str] = set()
        for ev in sorted(group, key=lambda e: (_parse_iso(e.when), e.event_id)):
            for c in ev.context_refs:
                if c not in seen_ctx:
                    seen_ctx.add(c)
                    ctx.append(c)
        # Sum friction signals.
        signals: dict[str, int] = {}
        for ev in group:
            for k, v in ev.friction_signals.items():
                signals[_normalize_signal(k)] = signals.get(
                    _normalize_signal(k), 0
                ) + int(v)
        merged = MemoryEvent(
            what=rep.what,
            when=earliest.when,
            event_id=rep.event_id,
            outcome=rep.outcome,
            importance=rep.importance,
            friction_signals=signals,
            category=rep.category,
            tags=tags,
            context_refs=ctx,
            metadata=dict(rep.metadata),
        )
        # Record provenance of the merge.
        merged.metadata.setdefault("merged_from", [e.event_id for e in group])
        merged.metadata.setdefault("merge_count", len(group))
        return merged

    # -- persistence ------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Serialize the whole store to a JSON-friendly dict."""
        return {
            "version": 1,
            "half_life_days": self.half_life_days,
            "events": [ev.to_dict() for ev in self.events.values()],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EpisodicMemoryStore:
        """Reconstruct a store from a serialized dict."""
        store = cls(half_life_days=data.get("half_life_days", DEFAULT_HALF_LIFE_DAYS))
        for ev_data in data.get("events", []):
            store.add(MemoryEvent.from_dict(ev_data))
        return store

    def save(self, path: str | Path) -> None:
        """Persist the store to a JSON file (atomic-ish, UTF-8 encoded)."""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        # ruff: PLW1514 — explicit encoding required by repo lint policy.
        p.write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: str | Path) -> EpisodicMemoryStore:
        """Load a store from a JSON file written by :meth:`save`."""
        p = Path(path)
        # ruff: PLW1514 — explicit encoding required by repo lint policy.
        data = json.loads(p.read_text(encoding="utf-8"))
        return cls.from_dict(data)

    # -- convenience ------------------------------------------------------

    def recompute_importance(
        self, *, weights: dict[str, float] | None = None, signal_scale: float = 3.0
    ) -> None:
        """Recompute and store the raw importance for every event in place."""
        for ev in self.events.values():
            ev.importance = ev.raw_importance(
                weights=weights, signal_scale=signal_scale
            )


# Re-export ``asdict`` for callers that want the dataclass view.
_ = asdict