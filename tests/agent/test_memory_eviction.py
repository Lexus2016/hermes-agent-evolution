"""Tests for ranked memory eviction with archive-not-delete (issue #123, slice A).

Covers the pure eviction module (``agent.memory_eviction``): ranking math,
capacity-trigger behaviour, determinism, and the archive-not-delete contract.
No store I/O, no network — stdlib + pytest only.
"""

import pytest

from agent.memory_eviction import (
    DEFAULT_ARCHIVE_NAMESPACE,
    DEFAULT_HALF_LIFE_DAYS,
    DEFAULT_WEIGHTS,
    EvictionPlan,
    MemoryEntry,
    MemoryEvictionPolicy,
    access_score,
    build_eviction_plan,
    max_access_count,
    provenance_score,
    rank_entries,
    recency_score,
    score_entry,
)

NOW = 1_800_000_000.0  # fixed clock for determinism
DAY = 86400.0


def entry(
    eid: str,
    age_days: float = 0.0,
    access_count: int = 0,
    trust_tier: str = "medium",
    source_class: str = "unknown",
    content: str = "entry content",
) -> MemoryEntry:
    created = NOW - age_days * DAY
    return MemoryEntry(
        id=eid,
        content=content,
        created_at=created,
        last_access=created,
        access_count=access_count,
        source_class=source_class,
        trust_tier=trust_tier,
    )


# ── scoring primitives ───────────────────────────────────────────────────────


def test_recency_score_half_life_halves_value():
    old = entry("old", age_days=DEFAULT_HALF_LIFE_DAYS)
    fresh = entry("fresh", age_days=0.0)
    assert recency_score(fresh, NOW) == pytest.approx(1.0)
    assert recency_score(old, NOW) == pytest.approx(0.5)
    assert recency_score(old, NOW) < recency_score(fresh, NOW)


def test_recency_score_clamps_negative_age():
    future = entry("future", age_days=-5.0)
    assert recency_score(future, NOW) == pytest.approx(1.0)


def test_access_score_normalizes_against_hottest():
    hot = entry("hot", access_count=100)
    cold = entry("cold", access_count=0)
    assert access_score(hot, max_access=100) == pytest.approx(1.0)
    assert access_score(cold, max_access=100) < access_score(hot, max_access=100)
    # Neutral when nothing has been accessed.
    assert access_score(cold, max_access=0) == pytest.approx(1.0)


def test_provenance_score_respects_tiers_and_sources():
    high = entry("h", trust_tier="high")
    low = entry("l", trust_tier="low")
    unknown = entry("u", trust_tier="totally-made-up")
    human_medium = entry("m", trust_tier="medium", source_class="human")
    medium = entry("mm", trust_tier="medium")
    assert provenance_score(high) > provenance_score(unknown) > provenance_score(low)
    # Source boost lifts a score but never pushes it past the 1.0 ceiling.
    assert provenance_score(human_medium) > provenance_score(medium)
    assert provenance_score(high) == pytest.approx(1.0)
    assert provenance_score(unknown) == pytest.approx(0.5)


def test_score_entry_combines_dimensions():
    fresh_high = entry("fh", age_days=0.0, access_count=10, trust_tier="high")
    old_low = entry("ol", age_days=300.0, access_count=0, trust_tier="low")
    assert score_entry(fresh_high, NOW, max_access=10) > score_entry(
        old_low, NOW, max_access=10
    )


# ── ranking ──────────────────────────────────────────────────────────────────


def test_rank_entries_sorts_descending_and_ties_break_on_id():
    a = entry("a", age_days=1.0)
    b = entry("b", age_days=2.0)
    c = entry("c", age_days=1.0)  # same age as a → tie on score
    ranked = rank_entries([b, c, a], NOW)
    assert [e.id for e, _ in ranked] == ["a", "c", "b"]
    scores = [s for _, s in ranked]
    assert scores == sorted(scores, reverse=True)


def test_rank_entries_is_deterministic():
    entries = [entry(f"e{i}", age_days=float(i % 7)) for i in range(20)]
    r1 = rank_entries(entries, NOW)
    r2 = rank_entries(list(reversed(entries)), NOW)
    assert [e.id for e, _ in r1] == [e.id for e, _ in r2]
    assert [s for _, s in r1] == [s for _, s in r2]


def test_max_access_count_empty_is_zero():
    assert max_access_count([]) == 0


# ── eviction plans (archive-not-delete) ──────────────────────────────────────


def test_under_cap_produces_empty_plan():
    entries = [entry("a"), entry("b")]
    usage = 100
    plan = build_eviction_plan(entries, cap_bytes=200, usage_bytes=usage, now=NOW)
    assert plan.is_empty
    assert plan.entries_to_archive == []
    assert plan.entries_to_keep == entries


def test_over_cap_evicts_lowest_value_until_under_cap():
    # 3 entries of equal size; old-low-trust one must go first.
    valuable = entry("valuable", age_days=0.0, access_count=50, trust_tier="high")
    mid = entry("mid", age_days=10.0, access_count=5, trust_tier="medium")
    stale = entry("stale", age_days=400.0, access_count=0, trust_tier="low")
    entries = [valuable, mid, stale]
    size = stale.size()
    usage = 3 * size
    plan = build_eviction_plan(entries, cap_bytes=2 * size, usage_bytes=usage, now=NOW)
    assert plan.entries_to_archive == [stale]
    assert plan.entries_to_keep == [valuable, mid]
    assert plan.freed_bytes == size
    assert plan.usage_bytes - plan.freed_bytes <= plan.target_bytes


def test_archive_not_delete_structural_contract():
    entries = [entry("a", age_days=1.0), entry("b", age_days=100.0)]
    size = entries[0].size()
    plan = build_eviction_plan(entries, cap_bytes=size, usage_bytes=2 * size, now=NOW)
    archived_ids = {e.id for e in plan.entries_to_archive}
    kept_ids = {e.id for e in plan.entries_to_keep}
    # Every entry appears exactly once across the two lists — nothing dropped.
    assert archived_ids | kept_ids == {"a", "b"}
    assert archived_ids & kept_ids == set()
    assert plan.archive_namespace == DEFAULT_ARCHIVE_NAMESPACE
    assert "archive" in plan.render_markdown().lower()


def test_invalid_cap_never_evicts():
    entries = [entry("a"), entry("b")]
    plan = build_eviction_plan(entries, cap_bytes=0, usage_bytes=10_000, now=NOW)
    assert plan.is_empty
    assert plan.entries_to_keep == entries


def test_empty_candidates_never_evict():
    plan = build_eviction_plan([], cap_bytes=10, usage_bytes=10_000, now=NOW)
    assert plan.is_empty
    assert plan.entries_to_keep == []


def test_plan_determinism_across_call_order():
    a = entry("a", age_days=3.0)
    b = entry("b", age_days=30.0)
    size = a.size()
    p1 = build_eviction_plan([a, b], cap_bytes=size, usage_bytes=2 * size, now=NOW)
    p2 = build_eviction_plan([b, a], cap_bytes=size, usage_bytes=2 * size, now=NOW)
    assert [e.id for e in p1.entries_to_archive] == [
        e.id for e in p2.entries_to_archive
    ]
    assert p1.freed_bytes == p2.freed_bytes


def test_access_frequency_protects_entries():
    # Same age, same tier: the frequently-accessed one must survive.
    hot = entry("hot", age_days=20.0, access_count=500)
    cold = entry("cold", age_days=20.0, access_count=0)
    size = hot.size()
    plan = build_eviction_plan(
        [hot, cold], cap_bytes=size, usage_bytes=2 * size, now=NOW
    )
    assert plan.entries_to_archive == [cold]
    assert plan.entries_to_keep == [hot]


# ── policy trigger (default-off) ─────────────────────────────────────────────


def test_policy_disabled_returns_none_even_over_cap():
    policy = MemoryEvictionPolicy(enabled=False, cap_bytes=100)
    assert policy.maybe_plan([entry("a")], usage_bytes=10_000, now=NOW) is None


def test_policy_enabled_under_cap_returns_none():
    policy = MemoryEvictionPolicy(enabled=True, cap_bytes=10_000)
    assert policy.maybe_plan([entry("a")], usage_bytes=100, now=NOW) is None


def test_policy_enabled_over_cap_returns_plan():
    policy = MemoryEvictionPolicy(enabled=True, cap_bytes=50)
    plan = policy.maybe_plan([entry("a")], usage_bytes=10_000, now=NOW)
    assert plan is not None
    assert not plan.is_empty


# ── serialization ────────────────────────────────────────────────────────────


def test_memory_entry_round_trip():
    e = entry("rt", age_days=2.0, access_count=7, trust_tier="high")
    restored = MemoryEntry.from_dict(e.to_dict())
    assert restored == e


def test_memory_entry_size_computed_when_absent():
    e = MemoryEntry(id="x", content="héllo", created_at=0.0, last_access=0.0)
    assert e.size() == len("héllo".encode())
    e2 = MemoryEntry(
        id="x", content="héllo", created_at=0.0, last_access=0.0, size_bytes=1
    )
    assert e2.size() == 1


def test_eviction_plan_is_empty_property():
    assert EvictionPlan().is_empty
