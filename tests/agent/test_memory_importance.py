"""Tests for memory importance scoring and episodic retrieval (#752).

Covers the pure scoring/retrieval module (``agent.memory_importance``) and
the wiring into ``agent.memory_manager.MemoryManager`` that makes the
scorer a real consumer (not dead code).

Only stdlib + pytest + unittest.mock. No live network calls.
"""

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

from agent.memory_importance import (  # noqa: E402
    DEFAULT_HALF_LIFE_DAYS,
    DEFAULT_SIGNAL_WEIGHTS,
    EpisodicMemoryStore,
    MemoryEvent,
    apply_temporal_decay,
    jaccard_similarity,
    score_importance,
    tokenize,
)
from agent.memory_manager import MemoryManager  # noqa: E402


# ── tokenize ──────────────────────────────────────────────────────────────────


def test_tokenize_basic():
    assert tokenize("Hello World") == {"hello", "world"}


def test_tokenize_empty():
    assert tokenize("") == set()


def test_tokenize_punctuation():
    assert tokenize("fix typo in README!") == {"fix", "typo", "in", "readme"}


def test_tokenize_case_insensitive():
    assert tokenize("FIX Typo") == {"fix", "typo"}


# ── jaccard_similarity ──────────────────────────────────────────────────────────


def test_jaccard_identical():
    assert jaccard_similarity({"a", "b"}, {"a", "b"}) == 1.0


def test_jaccard_disjoint():
    assert jaccard_similarity({"a"}, {"b"}) == 0.0


def test_jaccard_partial():
    assert jaccard_similarity({"a", "b"}, {"b", "c"}) == pytest.approx(1 / 3)


def test_jaccard_both_empty():
    assert jaccard_similarity(set(), set()) == 0.0


# ── score_importance ────────────────────────────────────────────────────────────


def test_score_zero_signals():
    assert score_importance({}) == 0.0


def test_score_single_signal_retries():
    s = score_importance({"retries": 10})
    assert 0.0 < s < 1.0
    # With signal_scale=3.0, score = 0.30 * (1 - exp(-10/3)) ≈ 0.30 * 0.9927 ≈ 0.298
    assert s == pytest.approx(0.30 * (1.0 - 0.0), abs=0.05)


def test_score_clamped_to_one():
    s = score_importance({
        "retries": 100,
        "human_corrections": 100,
        "task_failures": 100,
        "explicit_saves": 100,
        "novelty_recency": 100,
    })
    assert s == pytest.approx(1.0)


def test_score_signal_alias():
    s1 = score_importance({"retry": 10})
    s2 = score_importance({"retries": 10})
    assert s1 == pytest.approx(s2)


def test_score_unknown_signal_ignored():
    s = score_importance({"unknown_signal": 100})
    assert s == 0.0


def test_score_weights_sum_to_one():
    assert sum(DEFAULT_SIGNAL_WEIGHTS.values()) == pytest.approx(1.0)


# ── apply_temporal_decay ──────────────────────────────────────────────────────────


def test_decay_no_time_passed():
    now = datetime.now(timezone.utc).isoformat()
    assert apply_temporal_decay(0.8, now, reference_time=now) == pytest.approx(0.8)


def test_decay_one_half_life():
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=DEFAULT_HALF_LIFE_DAYS)
    result = apply_temporal_decay(0.8, old, reference_time=now)
    assert result == pytest.approx(0.4, rel=1e-2)


def test_decay_future_event_no_boost():
    now = datetime.now(timezone.utc)
    future = now + timedelta(days=10)
    assert apply_temporal_decay(0.5, future, reference_time=now) == pytest.approx(0.5)


def test_decay_invalid_half_life():
    with pytest.raises(ValueError):
        apply_temporal_decay(0.5, "2025-01-01T00:00:00Z", half_life_days=0)


# ── MemoryEvent ────────────────────────────────────────────────────────────────────


def test_event_defaults():
    ev = MemoryEvent(what="test event")
    assert ev.what == "test event"
    assert ev.importance == 0.0
    assert ev.tags == []
    assert ev.friction_signals == {}


def test_event_importance_clamped():
    ev = MemoryEvent(what="x", importance=1.5)
    assert ev.importance == 1.0
    ev2 = MemoryEvent(what="x", importance=-0.5)
    assert ev2.importance == 0.0


def test_event_to_dict_from_dict_roundtrip():
    ev = MemoryEvent(
        what="deploy failed",
        when="2025-01-01T12:00:00+00:00",
        outcome="rolled back",
        importance=0.7,
        friction_signals={"retries": 3},
        category="deployment",
        tags=["prod", "incident"],
        context_refs=["ctx1"],
        metadata={"source": "test"},
    )
    d = ev.to_dict()
    ev2 = MemoryEvent.from_dict(d)
    assert ev2.what == ev.what
    assert ev2.when == ev.when
    assert ev2.importance == ev.importance
    assert ev2.tags == ev.tags
    assert ev2.friction_signals == ev.friction_signals
    assert ev2.metadata == ev.metadata


def test_event_from_dict_ignores_unknown_keys():
    ev = MemoryEvent.from_dict({"what": "x", "unknown_key": "ignored"})
    assert ev.what == "x"


def test_event_to_json_from_json():
    ev = MemoryEvent(what="test", importance=0.5)
    j = ev.to_json()
    ev2 = MemoryEvent.from_json(j)
    assert ev2.what == "test"
    assert ev2.importance == 0.5


def test_event_raw_importance():
    ev = MemoryEvent(what="x", friction_signals={"retries": 5})
    assert ev.raw_importance() > 0.0


def test_event_decayed_importance():
    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=60)).isoformat()
    ev = MemoryEvent(what="x", when=old, friction_signals={"retries": 5})
    raw = ev.raw_importance()
    decayed = ev.decayed_importance(reference_time=now)
    assert decayed < raw


def test_event_tokens():
    ev = MemoryEvent(what="deploy failed", outcome="rolled back", tags=["prod"])
    toks = ev.tokens()
    assert "deploy" in toks
    assert "failed" in toks
    assert "rolled" in toks
    assert "prod" in toks


# ── EpisodicMemoryStore ────────────────────────────────────────────────────────────


def test_store_add_and_get():
    store = EpisodicMemoryStore()
    ev = MemoryEvent(what="test")
    store.add(ev)
    assert len(store) == 1
    assert store.get(ev.event_id) is not None


def test_store_remove():
    store = EpisodicMemoryStore()
    ev = MemoryEvent(what="test")
    store.add(ev)
    assert store.remove(ev.event_id) is True
    assert len(store) == 0
    assert store.remove("nonexistent") is False


def test_store_all():
    store = EpisodicMemoryStore()
    e1 = MemoryEvent(what="a")
    e2 = MemoryEvent(what="b")
    store.add_many([e1, e2])
    assert len(store.all()) == 2


def test_store_contains():
    store = EpisodicMemoryStore()
    ev = MemoryEvent(what="test")
    store.add(ev)
    assert ev.event_id in store
    assert "nonexistent" not in store


# ── Retrieval ──────────────────────────────────────────────────────────────────────


def test_retrieve_by_time_range():
    store = EpisodicMemoryStore()
    e1 = MemoryEvent(what="old", when="2025-01-01T00:00:00+00:00")
    e2 = MemoryEvent(what="mid", when="2025-06-01T00:00:00+00:00")
    e3 = MemoryEvent(what="new", when="2025-12-01T00:00:00+00:00")
    store.add_many([e1, e2, e3])
    result = store.retrieve_by_time_range("2025-03-01", "2025-09-01")
    assert len(result) == 1
    assert result[0].what == "mid"


def test_retrieve_by_category():
    store = EpisodicMemoryStore()
    e1 = MemoryEvent(what="a", category="deploy")
    e2 = MemoryEvent(what="b", category="debug")
    e3 = MemoryEvent(what="c", category="deploy", tags=["prod"])
    store.add_many([e1, e2, e3])
    result = store.retrieve_by_category(category="deploy")
    assert len(result) == 2
    result_tags = store.retrieve_by_category(tags=["prod"])
    assert len(result_tags) == 1


def test_retrieve_by_importance():
    store = EpisodicMemoryStore()
    e1 = MemoryEvent(what="low", importance=0.1)
    e2 = MemoryEvent(what="high", importance=0.9)
    e3 = MemoryEvent(what="mid", importance=0.5)
    store.add_many([e1, e2, e3])
    result = store.retrieve_by_importance(threshold=0.4)
    assert len(result) == 2
    assert result[0].importance >= result[1].importance


def test_text_search():
    store = EpisodicMemoryStore()
    e1 = MemoryEvent(what="deploy failed in production", outcome="rolled back")
    e2 = MemoryEvent(what="fixed typo in readme", outcome="merged")
    store.add_many([e1, e2])
    result = store.text_search("deploy production")
    assert len(result) >= 1
    assert result[0].what == "deploy failed in production"


def test_text_search_empty_query():
    store = EpisodicMemoryStore()
    e1 = MemoryEvent(what="test")
    store.add(e1)
    assert store.text_search("") == []


def test_retrieve_by_temporal_proximity():
    store = EpisodicMemoryStore()
    e1 = MemoryEvent(what="far", when="2025-01-01T00:00:00+00:00")
    e2 = MemoryEvent(what="close", when="2025-06-05T00:00:00+00:00")
    e3 = MemoryEvent(what="mid", when="2025-06-01T00:00:00+00:00")
    store.add_many([e1, e2, e3])
    result = store.retrieve_by_temporal_proximity("2025-06-03T00:00:00+00:00", limit=2)
    assert len(result) == 2
    # Closest first
    assert result[0].what in ("close", "mid")


# ── Deduplication ──────────────────────────────────────────────────────────────────


def test_deduplicate_merges_similar():
    store = EpisodicMemoryStore()
    e1 = MemoryEvent(what="deploy failed in production", importance=0.5, tags=["prod"])
    e2 = MemoryEvent(
        what="deploy failed in production", importance=0.8, tags=["incident"]
    )
    store.add_many([e1, e2])
    merged = store.deduplicate(threshold=0.5)
    assert len(merged) == 1
    assert merged[0].importance == 0.8
    assert "prod" in merged[0].tags
    assert "incident" in merged[0].tags


def test_deduplicate_keeps_distinct():
    store = EpisodicMemoryStore()
    e1 = MemoryEvent(what="deploy failed")
    e2 = MemoryEvent(what="fixed typo in readme")
    store.add_many([e1, e2])
    merged = store.deduplicate(threshold=0.8)
    assert len(merged) == 2


def test_deduplicate_inplace():
    store = EpisodicMemoryStore()
    e1 = MemoryEvent(what="duplicate event", importance=0.3)
    e2 = MemoryEvent(what="duplicate event", importance=0.6)
    store.add_many([e1, e2])
    store.deduplicate(threshold=0.5, inplace=True)
    assert len(store) == 1


def test_deduplicate_not_inplace():
    store = EpisodicMemoryStore()
    e1 = MemoryEvent(what="duplicate event", importance=0.3)
    e2 = MemoryEvent(what="duplicate event", importance=0.6)
    store.add_many([e1, e2])
    merged = store.deduplicate(threshold=0.5, inplace=False)
    assert len(merged) == 1
    assert len(store) == 2  # unchanged


# ── Persistence ──────────────────────────────────────────────────────────────────────


def test_save_and_load():
    store = EpisodicMemoryStore(half_life_days=45)
    store.add(MemoryEvent(what="event 1", importance=0.5, category="test"))
    store.add(MemoryEvent(what="event 2", importance=0.8, tags=["tag1"]))
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        path = f.name
    try:
        store.save(path)
        loaded = EpisodicMemoryStore.load(path)
        assert len(loaded) == 2
        assert loaded.half_life_days == 45
        events = loaded.all()
        whats = {e.what for e in events}
        assert "event 1" in whats
        assert "event 2" in whats
    finally:
        os.unlink(path)


def test_to_dict_from_dict_store():
    store = EpisodicMemoryStore()
    store.add(MemoryEvent(what="x", importance=0.5))
    d = store.to_dict()
    assert "version" in d
    assert "events" in d
    store2 = EpisodicMemoryStore.from_dict(d)
    assert len(store2) == 1


def test_recompute_importance():
    store = EpisodicMemoryStore()
    ev = MemoryEvent(what="x", friction_signals={"retries": 5}, importance=0.0)
    store.add(ev)
    store.recompute_importance()
    assert store.get(ev.event_id).importance > 0.0


# ── MemoryManager wiring (#752) ──────────────────────────────────────────────────────
# These tests verify the scorer is a REAL consumer (not dead code): the
# memory manager actually invokes score_importance() and records events.


def test_memory_manager_has_episodic_store():
    """The manager exposes an EpisodicMemoryStore instance."""
    mm = MemoryManager()
    assert isinstance(mm.episodic_store, EpisodicMemoryStore)
    assert len(mm.episodic_store) == 0


def test_score_memories_invokes_score_importance():
    """score_memories() actually calls score_importance (real call site)."""
    mm = MemoryManager()
    with mock.patch(
        "agent.memory_manager.score_importance", wraps=score_importance
    ) as spy:
        ev = mm.score_memories(
            "No, that is wrong, actually use the other approach",
            "Sorry, I failed to do that",
            session_id="s1",
        )
    assert spy.called, "score_memories did not invoke score_importance"
    assert ev.importance > 0.0, "wired score must be non-zero for a friction-heavy turn"
    assert len(mm.episodic_store) == 1
    recorded = mm.episodic_store.all()[0]
    assert recorded.importance == ev.importance
    assert recorded.category == "turn"
    assert "s1" in recorded.tags


def test_score_memories_zero_for_benign_turn():
    """A friction-free turn scores zero — the wiring must not fabricate signals."""
    mm = MemoryManager()
    ev = mm.score_memories("hello", "here is your answer")
    assert ev.importance == 0.0
    assert ev.friction_signals == {}


def test_score_memories_detects_tool_retries_and_saves():
    """Friction signals are derived from the message list (retries + saves)."""
    mm = MemoryManager()
    messages = [
        {"role": "user", "content": "please save this"},
        {"role": "assistant", "content": "okay", "tool_calls": [
            {"function": {"name": "memory"}},
        ]},
        {"role": "tool", "content": "error: disk full"},
        {"role": "tool", "content": "failed to write"},
    ]
    ev = mm.score_memories("please save this", "okay", messages=messages)
    assert ev.friction_signals.get("retries") == 2
    assert ev.friction_signals.get("explicit_saves") == 1


def test_sync_all_records_episodic_event():
    """sync_all() (the turn-sync hook) records a scored event in the store.

    Scoring runs inline (synchronous, no network) so the event is recorded
    immediately after sync_all() returns, before flush_pending().
    """
    mm = MemoryManager()
    # No providers registered → provider fan-out is skipped, but the
    # score_memories() call still fires inline and records an event.
    mm.sync_all(
        "No, wrong, actually do it differently",
        "Sorry, I failed; error occurred",
        session_id="sess-xyz",
    )
    assert len(mm.episodic_store) == 1, "sync_all did not record an episodic event"
    ev = mm.episodic_store.all()[0]
    assert ev.importance > 0.0
    assert "sess-xyz" in ev.tags


def test_score_memories_does_not_block_on_provider_failure():
    """A scoring failure inside sync_all must not prevent provider sync."""
    mm = MemoryManager()
    # Force score_memories to raise; provider sync should still run.
    with mock.patch.object(mm, "score_memories", side_effect=RuntimeError("boom")):
        called = {"sync": False}

        class _Provider:
            name = "test"

            def sync_turn(self, *a, **kw):
                called["sync"] = True

            def get_tool_schemas(self):
                return []

        mm.add_provider(_Provider())
        mm.sync_all("hello", "hi", session_id="s")
        assert mm.flush_pending(timeout=5.0)
    assert called["sync"], "provider sync_turn was not called when scoring raised"