"""Tests for :mod:`agent.memory_contradiction` (issue #37).

Covers the negation-flip heuristic: contradictions are flagged when a new
observation and a stored event share a subject but disagree on polarity,
and — just as important — non-contradictions (no shared subject, same
polarity, empty input) never flag. Also covers the real call-site wiring
in :class:`agent.memory_manager.MemoryManager`: scoring a turn that
contradicts an existing episodic event surfaces a flag, and the flag is
logged rather than crashing or deleting anything.
"""

from __future__ import annotations

import logging

from agent.memory_contradiction import (
    ContradictionFlag,
    detect_contradictions,
)
from agent.memory_importance import EpisodicMemoryStore, MemoryEvent


def _event(what: str, *, importance: float = 0.5, outcome: str = "") -> MemoryEvent:
    return MemoryEvent(what=what, outcome=outcome, importance=importance)


class TestDetectContradictions:
    def test_negation_flip_on_shared_subject_flags(self):
        stored = [_event("the database server is healthy and serving traffic")]
        flags = detect_contradictions(stored, "the database server is not healthy")
        assert len(flags) == 1
        flag = flags[0]
        assert flag.event_id == stored[0].event_id
        assert "database" in flag.reason
        assert flag.stored_when  # timestamp metadata present
        assert flag.detected_at

    def test_reverse_polarity_also_flags(self):
        stored = [_event("the printer is not connected to the network")]
        flags = detect_contradictions(
            stored, "the printer is connected to the network now"
        )
        assert len(flags) == 1
        assert "negated claim" in flags[0].reason

    def test_shared_subject_without_polarity_change_never_flags(self):
        stored = [_event("the solar inverter reports 12 kW output")]
        assert (
            detect_contradictions(
                stored, "the solar inverter reports 12 kW output again"
            )
            == []
        )
        assert (
            detect_contradictions(stored, "the solar inverter firmware is updating")
            == []
        )

    def test_no_shared_subject_never_flags(self):
        stored = [_event("the cat is sleeping on the sofa")]
        assert detect_contradictions(stored, "the dog is not sleeping") == []

    def test_empty_inputs(self):
        stored = [_event("anything at all")]
        assert detect_contradictions(stored, "") == []
        assert detect_contradictions(stored, "   ") == []
        assert detect_contradictions([], "something is not true") == []

    def test_limit_respects_most_recent_window(self):
        old = _event("the backup service is running")
        new = _event("the backup service is not running", importance=0.9)
        # Only the most recent event is in the window.
        flags = detect_contradictions(
            [old, new], "the backup service is running", limit=1
        )
        assert len(flags) == 1
        assert flags[0].event_id == new.event_id

    def test_min_importance_filter(self):
        low = _event("the modem is online", importance=0.1)
        high = _event("the modem is online", importance=0.9)
        flags = detect_contradictions(
            [low, high], "the modem is not online", min_importance=0.5
        )
        assert len(flags) == 1
        assert flags[0].event_id == high.event_id

    def test_confidence_bounded(self):
        stored = [_event("the api gateway is reachable from the office network")]
        flags = detect_contradictions(stored, "the api gateway is not reachable")
        assert 0.0 < flags[0].confidence <= 0.9

    def test_contraction_negation_detected(self):
        stored = [_event("the heat pump is working")]
        flags = detect_contradictions(stored, "the heat pump isn't working at all")
        assert len(flags) == 1

    def test_flag_carries_both_sides(self):
        stored = [_event("the samba share is mounted")]
        flags = detect_contradictions(stored, "the samba share is not mounted")
        assert flags[0].stored_text == "the samba share is mounted"
        assert flags[0].observation == "the samba share is not mounted"

    def test_never_raises_on_malformed_events(self):
        flags = detect_contradictions(
            [_event("the tv is on"), "not-an-event", None],  # type: ignore[list-item]
            "the tv is not on",
        )
        # Malformed entries are skipped defensively — at minimum the valid
        # event's flag (if any) is returned and nothing crashes.
        assert isinstance(flags, list)
        assert all(isinstance(f, ContradictionFlag) for f in flags)


class TestMemoryManagerWiring:
    """The real call site: MemoryManager.score_memories surfaces flags."""

    def _manager_with_event(self, what: str) -> tuple:
        from agent.memory_manager import MemoryManager

        manager = MemoryManager()
        manager.episodic_store.add(_event(what, importance=0.8))
        return manager, what

    def test_score_memories_surfaces_contradiction(self, caplog):
        manager, _stored = self._manager_with_event("the nas is healthy")
        with caplog.at_level(logging.WARNING, logger="agent.memory_manager"):
            manager.score_memories("the nas is not healthy", "ok", session_id="s1")
        assert any("memory contradiction" in r.message for r in caplog.records)
        # Both sides surfaced, nothing deleted.
        assert len(manager.episodic_store.events) == 2

    def test_check_contradictions_returns_flags(self):
        manager, _ = self._manager_with_event("the nas is healthy")
        flags = manager.check_contradictions("the nas is not healthy")
        assert len(flags) == 1
        assert flags[0].event_id in manager.episodic_store.events

    def test_no_false_positive_on_unrelated_turn(self, caplog):
        manager, _ = self._manager_with_event("the nas is healthy")
        with caplog.at_level(logging.WARNING, logger="agent.memory_manager"):
            manager.score_memories(
                "what is the weather in cape town", "sunny", session_id="s1"
            )
        assert not any("memory contradiction" in r.message for r in caplog.records)
        assert len(manager.episodic_store.events) == 2

    def test_empty_observation_never_flags(self):
        from agent.memory_manager import MemoryManager

        manager = MemoryManager()
        manager.episodic_store.add(_event("the nas is healthy"))
        assert manager.check_contradictions("") == []
        assert manager.check_contradictions("   ") == []
