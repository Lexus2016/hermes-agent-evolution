"""Ranked memory eviction with archive-not-delete semantics (issue #123, slice A).

The memory store has a write-everything-then-panic history: capacity pressure
blocks downstream consumers (the nightly dreaming pipeline hard-blocked twice
in Aug 2026 on a full store) because there is no *eviction* policy — only
write-path filters (``agent.memory_addition_gate``, #1270) and a capacity
guard (#129). This module adds the missing "learned forgetting" half: it
ranks existing entries and selects the lowest-value tail for archiving when
the store approaches its cap.

Design contract (mirrors :mod:`agent.memory_guard` and
:mod:`agent.memory_staleness` deliberately):

* **Pure and side-effect free.** The module never writes to disk, never
  mutates the store, and never logs. It returns an :class:`EvictionPlan`;
  the caller owns I/O and archiving (archive-not-delete is enforced by the
  plan's shape — evicted entries are handed back in an ``archive`` list,
  never a "delete" list, and never discarded).
* **Deterministic.** Same entries + same parameters always yield the same
  plan. All clock inputs (``now``) are explicit parameters; ties break on
  the entry ``id``. No randomness, no network.
* **Default-off.** :meth:`MemoryEvictionPolicy.maybe_plan` returns ``None``
  unless explicitly enabled *and* usage exceeds the cap. Existing store
  behaviour is untouched until a caller opts in.

Ranking (per the issue: recency × access × provenance):

* *recency* — exponential temporal decay over a configurable half-life
  (same shape as ``agent.memory_importance.apply_temporal_decay``);
* *access* — log-scaled access frequency, normalized against the hottest
  entry in the candidate set;
* *provenance* — ``trust_tier`` weight (high/medium/low) scaled by an
  optional ``source_class`` boost, reusing the #316 provenance tags.

The combined score is a multiplicative blend (``recency**w_r *
access**w_a * provenance**w_p``, weights summing to 1): a single weak
dimension sinks the entry, which is exactly what eviction wants.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# Default ranking weights (sum to 1.0). Recency dominates; provenance is the
# smallest lever so that a low-trust entry only loses ties, it does not get
# evicted purely for its origin while a same-age entry survives.
DEFAULT_WEIGHTS: dict[str, float] = {
    "recency": 0.5,
    "access": 0.3,
    "provenance": 0.2,
}

# Trust-tier weights for the provenance factor (#316 tags). Unknown tiers
# land at 0.5 — treated as "no evidence", not "hostile".
TRUST_TIER_WEIGHTS: dict[str, float] = {
    "high": 1.0,
    "medium": 0.6,
    "low": 0.3,
}
UNKNOWN_TRUST_TIER_WEIGHT: float = 0.5

# Optional per-source multipliers on top of the trust tier. Unknown sources
# are neutral (1.0) — the module never guesses a source's trustworthiness.
SOURCE_BOOSTS: dict[str, float] = {
    "human": 1.1,
    "user": 1.1,
    "agent": 0.9,
    "system": 0.9,
}
UNKNOWN_SOURCE_BOOST: float = 1.0

DEFAULT_HALF_LIFE_DAYS: float = 30.0

# Where archiving is expected to land (caller-owned; the plan only names it).
DEFAULT_ARCHIVE_NAMESPACE: str = "archive"


@dataclass(frozen=True)
class MemoryEntry:
    """A single memory-store entry as seen by the eviction policy.

    The schema is deliberately store-agnostic: callers map their store rows
    (tqmemory notes, MEMORY.md blocks, provider records) onto these fields.
    ``size_bytes`` is computed from ``content`` when not supplied.
    """

    id: str
    content: str
    created_at: float  # epoch seconds
    last_access: float  # epoch seconds; may equal created_at
    access_count: int = 0
    source_class: str = "unknown"
    trust_tier: str = "unknown"
    size_bytes: Optional[int] = None

    def size(self) -> int:
        if self.size_bytes is not None:
            return max(0, self.size_bytes)
        return len(self.content.encode("utf-8"))

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "content": self.content,
            "created_at": self.created_at,
            "last_access": self.last_access,
            "access_count": self.access_count,
            "source_class": self.source_class,
            "trust_tier": self.trust_tier,
        }
        if self.size_bytes is not None:
            d["size_bytes"] = self.size_bytes
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> MemoryEntry:
        return cls(
            id=str(d["id"]),
            content=str(d.get("content", "")),
            created_at=float(d.get("created_at", 0.0)),
            last_access=float(d.get("last_access", d.get("created_at", 0.0))),
            access_count=int(d.get("access_count", 0)),
            source_class=str(d.get("source_class", "unknown")),
            trust_tier=str(d.get("trust_tier", "unknown")),
            size_bytes=(
                int(d["size_bytes"]) if d.get("size_bytes") is not None else None
            ),
        )


def _age_days(entry: MemoryEntry, now: float) -> float:
    """Age of the entry in fractional days (clamped at 0)."""
    return max(0.0, (now - entry.created_at) / 86400.0)


def recency_score(
    entry: MemoryEntry,
    now: float,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
) -> float:
    """Exponential temporal decay: 2 ** (-age/half-life), in [0, 1]."""
    if half_life_days <= 0:
        return 0.0
    return 2.0 ** (-_age_days(entry, now) / half_life_days)


def access_score(entry: MemoryEntry, max_access: int) -> float:
    """Log-scaled access frequency normalized against the hottest entry.

    A neutral 1.0 when no entry in the candidate set has been accessed
    (nothing differentiates on access, so the factor must not punish).
    """
    if max_access <= 0:
        return 1.0
    return min(1.0, math.log1p(max(0, entry.access_count)) / math.log1p(max_access))


def provenance_score(entry: MemoryEntry) -> float:
    """Trust-tier weight scaled by an optional source-class boost."""
    tier = TRUST_TIER_WEIGHTS.get(entry.trust_tier, UNKNOWN_TRUST_TIER_WEIGHT)
    boost = SOURCE_BOOSTS.get(entry.source_class, UNKNOWN_SOURCE_BOOST)
    return min(1.0, max(0.0, tier * boost))


def score_entry(
    entry: MemoryEntry,
    now: float,
    max_access: int = 0,
    weights: Optional[dict[str, float]] = None,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
) -> float:
    """Multiplicative blend of recency × access × provenance, in [0, 1].

    ``max_access`` must be the maximum ``access_count`` across the candidate
    set (callers compute it once via :func:`max_access_count`); passing 0
    makes the access factor neutral.
    """
    w = dict(DEFAULT_WEIGHTS if weights is None else weights)
    w_sum = sum(w.values()) or 1.0
    r = w.get("recency", 0.0) / w_sum
    a = w.get("access", 0.0) / w_sum
    p = w.get("provenance", 0.0) / w_sum
    rec = recency_score(entry, now, half_life_days)
    acc = access_score(entry, max_access)
    prov = provenance_score(entry)
    return (rec**r) * (acc**a) * (prov**p)


def max_access_count(entries: list[MemoryEntry]) -> int:
    """Largest access count in the candidate set (0 when empty)."""
    return max((e.access_count for e in entries), default=0)


def rank_entries(
    entries: list[MemoryEntry],
    now: float,
    weights: Optional[dict[str, float]] = None,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
) -> list[tuple[MemoryEntry, float]]:
    """Rank entries by score, highest (most valuable) first.

    Ties break on ``id`` ascending so the ranking is a pure function of its
    inputs. Entries are never modified; the caller owns the store.
    """
    max_acc = max_access_count(entries)
    scored = [
        (
            entry,
            score_entry(
                entry,
                now=now,
                max_access=max_acc,
                weights=weights,
                half_life_days=half_life_days,
            ),
        )
        for entry in entries
    ]
    scored.sort(key=lambda t: (-t[1], t[0].id))
    return scored


@dataclass(frozen=True)
class EvictionPlan:
    """What to archive and what to keep, plus why.

    Archive-not-delete is structural: the plan carries the evicted entries in
    ``entries_to_archive`` (never a "delete" list) and names the archive
    destination; the caller decides how to move them there. Nothing in this
    module discards data.
    """

    entries_to_archive: list[MemoryEntry] = field(default_factory=list)
    entries_to_keep: list[MemoryEntry] = field(default_factory=list)
    freed_bytes: int = 0
    target_bytes: int = 0
    usage_bytes: int = 0
    reason: str = ""
    archive_namespace: str = DEFAULT_ARCHIVE_NAMESPACE

    @property
    def is_empty(self) -> bool:
        return not self.entries_to_archive

    def render_markdown(self) -> str:
        """Human-readable summary for logs/reports (no store writes)."""
        if self.is_empty:
            return (
                f"Eviction plan: nothing to archive "
                f"(usage {self.usage_bytes}B <= cap {self.target_bytes}B). "
                f"{self.reason}".strip()
            )
        lines = [
            f"Eviction plan: archive {len(self.entries_to_archive)} entries "
            f"to '{self.archive_namespace}' (frees ~{self.freed_bytes}B).",
            f"Reason: {self.reason}",
        ]
        for e in self.entries_to_archive:
            lines.append(f"  - {e.id} ({e.size()}B, trust={e.trust_tier})")
        return "\n".join(lines)


def build_eviction_plan(
    entries: list[MemoryEntry],
    cap_bytes: int,
    usage_bytes: int,
    now: Optional[float] = None,
    weights: Optional[dict[str, float]] = None,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
    archive_namespace: str = DEFAULT_ARCHIVE_NAMESPACE,
) -> EvictionPlan:
    """Select the lowest-value tail for archiving when usage exceeds the cap.

    * usage <= cap → empty plan (no eviction needed).
    * cap <= 0 → empty plan (misconfiguration; never destroy data on a bad
      cap, and never archive everything because a caller passed 0).
    * otherwise → evict lowest-scoring entries (ascending rank) until the
      projected usage drops to the cap, or the candidate set is exhausted.

    ``now`` defaults to ``time.time()`` but should be passed explicitly by
    callers that need reproducible plans (tests, dry runs).
    """
    now_ts = time.time() if now is None else now
    empty_reason = (
        f"usage {usage_bytes}B <= cap {cap_bytes}B"
        if usage_bytes <= cap_bytes
        else f"invalid cap {cap_bytes}B"
    )
    if not entries or usage_bytes <= cap_bytes or cap_bytes <= 0:
        return EvictionPlan(
            entries_to_archive=[],
            entries_to_keep=list(entries),
            freed_bytes=0,
            target_bytes=cap_bytes,
            usage_bytes=usage_bytes,
            reason="nothing to evict: " + empty_reason,
            archive_namespace=archive_namespace,
        )

    ranked = rank_entries(
        entries, now=now_ts, weights=weights, half_life_days=half_life_days
    )
    # Ascending value: evict the least valuable first.
    ranked_asc = list(reversed(ranked))
    projected = usage_bytes
    archive: list[MemoryEntry] = []
    for entry, _score in ranked_asc:
        if projected <= cap_bytes:
            break
        archive.append(entry)
        projected -= entry.size()
        projected = max(projected, 0)

    archived_ids = {e.id for e in archive}
    keep = [e for e in entries if e.id not in archived_ids]
    return EvictionPlan(
        entries_to_archive=archive,
        entries_to_keep=keep,
        freed_bytes=usage_bytes - projected,
        target_bytes=cap_bytes,
        usage_bytes=usage_bytes,
        reason=(
            f"usage {usage_bytes}B exceeds cap {cap_bytes}B; archiving "
            f"{len(archive)} lowest-value entr{'y' if len(archive) == 1 else 'ies'}"
        ),
        archive_namespace=archive_namespace,
    )


@dataclass(frozen=True)
class MemoryEvictionPolicy:
    """Capacity trigger with a hard default-off contract.

    The store's write path can call :meth:`maybe_plan` at capacity pressure;
    it returns ``None`` unless eviction is explicitly enabled AND usage
    exceeds the cap — so existing store behaviour is byte-identical until a
    caller opts in.
    """

    enabled: bool = False
    cap_bytes: int = 0
    weights: Optional[dict[str, float]] = None
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS
    archive_namespace: str = DEFAULT_ARCHIVE_NAMESPACE

    def maybe_plan(
        self,
        entries: list[MemoryEntry],
        usage_bytes: int,
        now: Optional[float] = None,
    ) -> Optional[EvictionPlan]:
        if not self.enabled or usage_bytes <= self.cap_bytes:
            return None
        return build_eviction_plan(
            entries,
            cap_bytes=self.cap_bytes,
            usage_bytes=usage_bytes,
            now=now,
            weights=self.weights,
            half_life_days=self.half_life_days,
            archive_namespace=self.archive_namespace,
        )
