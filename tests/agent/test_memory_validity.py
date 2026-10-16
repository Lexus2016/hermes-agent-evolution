"""Tests for TEPA revocable memory state (#154).

Covers the explicit-validity extension of the episodic tier
(``agent.memory_importance``): a stored event contradicted by fresh
evidence is *revoked* (marked with validity state, superseding id and
reason) rather than overwritten or left at full weight; revocation is
queryable; and revoked entries never resurface in normal retrieval.

Only stdlib + pytest + unittest.mock. No live network calls.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone, UTC

import pytest

from agent.memory_importance import (
    REVOCATION_REASON_CONTRADICTION,
    VALIDITY_ACTIVE,
    VALIDITY_REVOKED,
    EpisodicMemoryStore,
    MemoryEvent,
)


def _event(
    what: str, *, importance: float = 0.7, when: str | None = None
) -> MemoryEvent:
    """Build a MemoryEvent with a fixed (or auto) timestamp."""
    if when is None:
        when = datetime.now(UTC).isoformat()
    return MemoryEvent(what=what, importance=importance, when=when)


class TestMemoryEventValidity:
    def test_default_validity_is_active(self):
        ev = _event("the nas is healthy")
        assert ev.validity == VALIDITY_ACTIVE
        assert not ev.is_revoked
        assert ev.revoked_at == ""
        assert ev.revoked_by == ""
        assert ev.revocation_reason == ""

    def test_serialization_roundtrip_preserves_validity(self):
        ev = _event("the nas is healthy")
        assert ev.to_dict()["validity"] == VALIDITY_ACTIVE
        clone = MemoryEvent.from_dict(ev.to_dict())
        assert clone.validity == VALIDITY_ACTIVE

    def test_revoked_state_serializes(self):
        ev = _event("the nas is healthy")
        ev.validity = VALIDITY_REVOKED
        ev.revoked_at = "2026-08-31T12:00:00+00:00"
        ev.revoked_by = "new-event-id"
        ev.revocation_reason = REVOCATION_REASON_CONTRADICTION
        clone = MemoryEvent.from_dict(ev.to_dict())
        assert clone.validity == VALIDITY_REVOKED
        assert clone.revoked_at == "2026-08-31T12:00:00+00:00"
        assert clone.revoked_by == "new-event-id"
        assert clone.revocation_reason == REVOCATION_REASON_CONTRADICTION

    def test_legacy_dict_without_validity_loads_active(self):
        # Old serializations (pre-#154) have no validity keys: they must load
        # as active so revocation is strictly opt-in state.
        legacy = {
            "event_id": "legacy-1",
            "what": "the nas is healthy",
            "when": "2026-01-01T00:00:00+00:00",
            "outcome": "",
            "importance": 0.7,
            "friction_signals": {},
            "category": "",
            "tags": [],
            "context_refs": [],
            "metadata": {},
        }
        ev = MemoryEvent.from_dict(legacy)
        assert ev.validity == VALIDITY_ACTIVE
        assert not ev.is_revoked


class TestStoreRevocation:
    def test_revoke_marks_event_and_keeps_record(self):
        store = EpisodicMemoryStore()
        old = _event("the nas is healthy")
        new = _event("the nas is not healthy")
        store.add(old)
        store.add(new)
        assert store.revoke(old.event_id, by_event_id=new.event_id) is True
        # Revoke != delete: the record survives, marked.
        assert old.event_id in store.events
        assert old.validity == VALIDITY_REVOKED
        assert old.is_revoked
        assert old.revoked_by == new.event_id
        assert old.revocation_reason == REVOCATION_REASON_CONTRADICTION
        assert old.revoked_at != ""

    def test_revoke_unknown_id_returns_false(self):
        store = EpisodicMemoryStore()
        assert store.revoke("does-not-exist") is False

    def test_revoke_is_idempotent(self):
        store = EpisodicMemoryStore()
        ev = _event("the nas is healthy")
        store.add(ev)
        assert store.revoke(ev.event_id) is True
        assert store.revoke(ev.event_id) is False  # already revoked

    def test_revoked_events_audit_listing(self):
        store = EpisodicMemoryStore()
        a = _event("stale ip is 10.0.0.99")
        b = _event("ip is 10.0.0.60")
        c = _event("unrelated")
        store.add(a)
        store.add(b)
        store.add(c)
        store.revoke(a.event_id, by_event_id=b.event_id)
        revoked = store.revoked_events()
        assert [e.event_id for e in revoked] == [a.event_id]
        entry = revoked[0]
        assert entry.revoked_by == b.event_id
        assert entry.revocation_reason == REVOCATION_REASON_CONTRADICTION
        assert store.active_events() == [b, c]


class TestRetrievalExcludesRevoked:
    def _store_with_revoked(self) -> EpisodicMemoryStore:
        store = EpisodicMemoryStore()
        old = _event("the nas is healthy", importance=0.9)
        new = _event("the nas is not healthy", importance=0.9)
        store.add(old)
        store.add(new)
        store.revoke(old.event_id, by_event_id=new.event_id)
        return store

    def test_text_search_skips_revoked_by_default(self):
        store = self._store_with_revoked()
        hit_texts = {e.what for e in store.text_search("nas")}
        assert "the nas is healthy" not in hit_texts
        assert "the nas is not healthy" in hit_texts

    def test_retrieval_methods_default_to_active_only(self):
        store = self._store_with_revoked()
        active_ids = {e.event_id for e in store.active_events()}
        # Every retrieval surface must exclude the revoked event by default.
        assert {e.event_id for e in store.text_search("nas")} == active_ids
        assert {e.event_id for e in store.retrieve_by_time_range()} == active_ids
        assert {e.event_id for e in store.retrieve_by_category()} == active_ids
        assert {e.event_id for e in store.retrieve_by_importance()} == active_ids
        assert {
            e.event_id
            for e in store.retrieve_by_temporal_proximity(store.active_events()[0].when)
        } == active_ids

    def test_include_revoked_flag_restores_audit_view(self):
        store = self._store_with_revoked()
        store_texts = {e.what for e in store.text_search("nas", include_revoked=True)}
        assert "the nas is healthy" in store_texts
        assert "the nas is not healthy" in store_texts

    def test_deduplicate_never_resurrects_revoked(self):
        store = EpisodicMemoryStore()
        old = _event("the nas is healthy")
        dup = _event("the nas is healthy and mounted", importance=0.9)
        store.add(old)
        store.add(dup)
        store.revoke(old.event_id, by_event_id=dup.event_id)
        store.deduplicate(threshold=0.5)
        # The revoked event must not be merged back into an active one.
        assert all(e.validity == VALIDITY_ACTIVE for e in store.active_events())
        assert old.event_id not in {e.event_id for e in store.active_events()}

    def test_save_load_preserves_revocation(self, tmp_path):
        store = self._store_with_revoked()
        path = tmp_path / "memory.json"
        store.save(path)
        loaded = EpisodicMemoryStore.load(path)
        revoked = loaded.revoked_events()
        assert len(revoked) == 1
        assert revoked[0].revoked_by == store.active_events()[0].event_id
        assert revoked[0].validity == VALIDITY_REVOKED


class TestWritePathRevocation:
    def test_score_memories_revokes_contradicted_stored_event(self):
        from agent.memory_manager import MemoryManager

        manager = MemoryManager()
        stored = _event("the nas is healthy", importance=0.8)
        manager.episodic_store.add(stored)
        manager.score_memories("the nas is not healthy", "ok", session_id="s1")
        # Stored event revoked, new event active, nothing deleted.
        assert stored.validity == VALIDITY_REVOKED
        assert stored.revoked_by in manager.episodic_store.events
        assert stored.revocation_reason == REVOCATION_REASON_CONTRADICTION
        assert len(manager.episodic_store.events) == 2
        assert len(manager.episodic_store.active_events()) == 1

    def test_unrelated_turn_revokes_nothing(self):
        from agent.memory_manager import MemoryManager

        manager = MemoryManager()
        stored = _event("the nas is healthy")
        manager.episodic_store.add(stored)
        manager.score_memories(
            "what is the weather in cape town", "sunny", session_id="s1"
        )
        assert stored.validity == VALIDITY_ACTIVE
        assert manager.episodic_store.revoked_events() == []

    def test_revocation_failure_is_non_fatal(self):
        from agent.memory_manager import MemoryManager

        manager = MemoryManager()
        stored = _event("the nas is healthy")
        manager.episodic_store.add(stored)

        def boom(event_id, **kwargs):
            raise RuntimeError("storage exploded")

        manager.episodic_store.revoke = boom  # type: ignore[method-assign]
        # The turn must still succeed even if revocation fails.
        ev = manager.score_memories("the nas is not healthy", "ok", session_id="s1")
        assert ev is not None
        assert ev in manager.episodic_store.events.values()

    def test_contradiction_scan_skips_revoked_events(self):
        from agent.memory_manager import MemoryManager

        manager = MemoryManager()
        stored = _event("the nas is healthy")
        manager.episodic_store.add(stored)
        manager.score_memories("the nas is not healthy", "ok", session_id="s1")
        # A second conflicting observation no longer flags the revoked entry:
        # dead memories do not generate noise.
        assert manager.check_contradictions("the nas is not healthy") == []
