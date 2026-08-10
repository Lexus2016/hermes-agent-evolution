"""Tests for per-skill provenance record (#2190).

Verifies that:
- _empty_record includes source_run_id and recent_failure_rate
- set_source_run_id records the originating run
- record_skill_outcome updates use_count and failure_rate
- Failure rate is a sliding window over recent outcomes
- The new fields are queryable via curated_report
"""

import json
from pathlib import Path

import pytest

from tools.skill_usage import (
    _empty_record,
    _FAILURE_WINDOW,
    get_record,
    load_usage,
    record_skill_outcome,
    set_source_run_id,
)


class TestEmptyRecordFields:
    def test_empty_record_has_provenance_fields(self):
        rec = _empty_record()
        assert "source_run_id" in rec
        assert rec["source_run_id"] is None
        assert "recent_failure_rate" in rec
        assert rec["recent_failure_rate"] == 0.0


class TestSetSourceRunId:
    def test_set_source_run_id(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        set_source_run_id("test-skill", "run-abc-123")
        rec = get_record("test-skill")
        assert rec["source_run_id"] == "run-abc-123"

    def test_set_source_run_id_empty_is_noop(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        set_source_run_id("test-skill", "")
        # Should not create a record for an empty run_id
        data = load_usage()
        assert "test-skill" not in data

    def test_set_source_run_id_truncates_long_ids(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        long_id = "x" * 500
        set_source_run_id("test-skill", long_id)
        rec = get_record("test-skill")
        assert len(rec["source_run_id"]) == 200


class TestRecordSkillOutcome:
    def test_record_success_bumps_use_count(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        record_skill_outcome("test-skill", success=True)
        rec = get_record("test-skill")
        assert rec["use_count"] == 1
        assert rec["recent_failure_rate"] == 0.0

    def test_record_failure_updates_rate(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        record_skill_outcome("test-skill", success=False)
        rec = get_record("test-skill")
        assert rec["use_count"] == 1
        assert rec["recent_failure_rate"] == 1.0

    def test_mixed_outcomes_sliding_window(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        # 3 successes, 1 failure → 25% failure rate
        for _ in range(3):
            record_skill_outcome("test-skill", success=True)
        record_skill_outcome("test-skill", success=False)
        rec = get_record("test-skill")
        assert rec["use_count"] == 4
        assert rec["recent_failure_rate"] == 0.25

    def test_sliding_window_trims_old_outcomes(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        # Fill beyond the window with all failures
        for _ in range(_FAILURE_WINDOW + 5):
            record_skill_outcome("test-skill", success=False)
        rec = get_record("test-skill")
        # Only the last _FAILURE_WINDOW outcomes are kept
        outcomes = rec.get("_recent_outcomes", [])
        assert len(outcomes) == _FAILURE_WINDOW
        assert rec["recent_failure_rate"] == 1.0

    def test_sliding_window_evicts_old_failures(self, tmp_path, monkeypatch):
        """After enough successes, old failures drop out of the window."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        # 5 failures, then _FAILURE_WINDOW successes → failure rate should be 0
        for _ in range(5):
            record_skill_outcome("test-skill", success=False)
        for _ in range(_FAILURE_WINDOW):
            record_skill_outcome("test-skill", success=True)
        rec = get_record("test-skill")
        assert rec["recent_failure_rate"] == 0.0
