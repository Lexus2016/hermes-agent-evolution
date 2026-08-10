"""Tests for post-compaction trajectory evaluation signal (#2185 Phase 1).

Verifies that:
- Compaction events are recorded to the sidecar JSON
- Post-compaction outcomes are recorded and matched to compaction events
- The eval summary correctly computes success rate
- Outcomes without a matching compaction event are not recorded
- The record cap prevents unbounded growth
"""

import json

import pytest

from agent.compaction_eval import (
    get_eval_summary,
    record_compaction_event,
    record_post_compaction_outcome,
    _eval_file,
    _load,
    _MAX_RECORDS,
)


class TestRecordCompactionEvent:
    def test_record_compaction_event(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        record_compaction_event(
            turn_id="turn-1",
            session_id="sess-1",
            messages_before=50,
            messages_after=15,
        )
        records = _load()
        assert len(records) == 1
        assert records[0]["event"] == "compaction"
        assert records[0]["turn_id"] == "turn-1"
        assert records[0]["messages_after"] == 15

    def test_empty_turn_id_is_noop(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        record_compaction_event(turn_id="", session_id="s")
        assert _load() == []


class TestRecordPostCompactionOutcome:
    def test_outcome_matched_to_compaction(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        record_compaction_event(turn_id="turn-1", session_id="s")
        record_post_compaction_outcome(turn_id="turn-1", success=True)
        records = _load()
        assert len(records) == 2
        assert records[1]["event"] == "outcome"
        assert records[1]["success"] is True

    def test_outcome_without_compaction_not_recorded(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        record_post_compaction_outcome(turn_id="turn-99", success=True)
        records = _load()
        assert len(records) == 0

    def test_failed_outcome(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        record_compaction_event(turn_id="turn-2", session_id="s")
        record_post_compaction_outcome(turn_id="turn-2", success=False, failed=True)
        records = _load()
        assert records[1]["failed"] is True
        assert records[1]["success"] is False


class TestEvalSummary:
    def test_summary_with_matched_events(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        # 2 compactions: 1 success, 1 failure
        record_compaction_event(turn_id="t1", session_id="s")
        record_post_compaction_outcome(turn_id="t1", success=True)
        record_compaction_event(turn_id="t2", session_id="s")
        record_post_compaction_outcome(turn_id="t2", success=False, failed=True)

        summary = get_eval_summary()
        assert summary["total_compactions"] == 2
        assert summary["matched"] == 2
        assert summary["successes"] == 1
        assert summary["failures"] == 1
        assert summary["success_rate"] == 0.5

    def test_summary_no_data(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        summary = get_eval_summary()
        assert summary["total_compactions"] == 0
        assert summary["success_rate"] is None

    def test_summary_unmatched_compaction(self, tmp_path, monkeypatch):
        """Compaction without outcome (turn still running) not counted."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        record_compaction_event(turn_id="t1", session_id="s")
        summary = get_eval_summary()
        assert summary["total_compactions"] == 1
        assert summary["matched"] == 0
        assert summary["success_rate"] is None


class TestRecordCap:
    def test_cap_prevents_unbounded_growth(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        # Write more than _MAX_RECORDS entries
        for i in range(_MAX_RECORDS + 50):
            record_compaction_event(turn_id=f"t{i}", session_id="s")
        records = _load()
        assert len(records) <= _MAX_RECORDS
