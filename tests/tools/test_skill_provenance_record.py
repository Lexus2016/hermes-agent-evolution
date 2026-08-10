"""Tests for tools/skill_provenance_record.py — per-skill provenance score (#2190).

Tests cover:
  - init_provenance_record sets the 4 required fields
  - record_invocation increments count + updates failure rate
  - sliding-window failure rate calculation
  - get_provenance_record returns defaults for missing skills
"""

from unittest.mock import patch

import pytest

from tools.skill_provenance_record import (
    init_provenance_record,
    record_invocation,
    get_provenance_record,
    _FAILURE_WINDOW,
)


class TestInitProvenanceRecord:
    def test_sets_all_fields(self):
        state = {}

        def fake_mutate(name, mutator, **kw):
            mutator(state)

        with patch("tools.skill_usage._mutate", side_effect=fake_mutate):
            init_provenance_record("test-skill", source_run_id="run-abc")

        assert state["source_run_id"] == "run-abc"
        assert state["created_at"] is not None
        assert state["invocation_count"] == 0
        assert state["recent_failure_rate"] == 0.0
        assert state["failure_history"] == []

    def test_auto_generates_run_id(self):
        state = {}

        def fake_mutate(name, mutator, **kw):
            mutator(state)

        with patch("tools.skill_usage._mutate", side_effect=fake_mutate):
            init_provenance_record("test-skill")

        assert state["source_run_id"]
        assert len(state["source_run_id"]) > 0

    def test_swallows_errors(self):
        with patch("tools.skill_usage._mutate", side_effect=RuntimeError):
            init_provenance_record("test")  # should not raise


class TestRecordInvocation:
    def test_increments_count(self):
        state = {"invocation_count": 5, "failure_history": []}

        def fake_mutate(name, mutator, **kw):
            mutator(state)

        with patch("tools.skill_usage._mutate", side_effect=fake_mutate):
            record_invocation("s")
        assert state["invocation_count"] == 6

    def test_failure_rate_all_success(self):
        state = {"invocation_count": 0, "failure_history": []}

        def fake_mutate(name, mutator, **kw):
            mutator(state)

        with patch("tools.skill_usage._mutate", side_effect=fake_mutate):
            for _ in range(10):
                record_invocation("s", failed=False)
        assert state["recent_failure_rate"] == 0.0
        assert state["invocation_count"] == 10

    def test_failure_rate_all_failures(self):
        state = {"invocation_count": 0, "failure_history": []}

        def fake_mutate(name, mutator, **kw):
            mutator(state)

        with patch("tools.skill_usage._mutate", side_effect=fake_mutate):
            for _ in range(10):
                record_invocation("s", failed=True)
        assert state["recent_failure_rate"] == 1.0

    def test_failure_rate_mixed(self):
        state = {"invocation_count": 0, "failure_history": []}

        def fake_mutate(name, mutator, **kw):
            mutator(state)

        with patch("tools.skill_usage._mutate", side_effect=fake_mutate):
            for i in range(10):
                record_invocation("s", failed=(i % 2 == 0))
        # 5 failures out of 10
        assert state["recent_failure_rate"] == 0.5

    def test_sliding_window_trims(self):
        state = {"invocation_count": 0, "failure_history": []}

        def fake_mutate(name, mutator, **kw):
            mutator(state)

        with patch("tools.skill_usage._mutate", side_effect=fake_mutate):
            for _ in range(_FAILURE_WINDOW + 10):
                record_invocation("s", failed=False)
        assert len(state["failure_history"]) == _FAILURE_WINDOW

    def test_sliding_window_recent_only(self):
        """Old failures drop out of the window."""
        state = {"invocation_count": 0, "failure_history": []}

        def fake_mutate(name, mutator, **kw):
            mutator(state)

        with patch("tools.skill_usage._mutate", side_effect=fake_mutate):
            # Fill window with failures
            for _ in range(_FAILURE_WINDOW):
                record_invocation("s", failed=True)
            # Now add successes that push failures out
            for _ in range(_FAILURE_WINDOW):
                record_invocation("s", failed=False)
        assert state["recent_failure_rate"] == 0.0


class TestGetProvenanceRecord:
    def test_returns_existing(self):
        mock_rec = {
            "source_run_id": "run-1",
            "created_at": "2026-01-01T00:00:00Z",
            "invocation_count": 42,
            "recent_failure_rate": 0.15,
            "failure_history": [0, 1, 0],
        }
        with patch("tools.skill_usage.get_record", return_value=mock_rec):
            rec = get_provenance_record("test")
        assert rec["source_run_id"] == "run-1"
        assert rec["invocation_count"] == 42
        assert rec["recent_failure_rate"] == 0.15

    def test_defaults_on_missing(self):
        with patch("tools.skill_usage.get_record", return_value={}):
            rec = get_provenance_record("nonexistent")
        assert rec["source_run_id"] is None
        assert rec["invocation_count"] == 0
        assert rec["recent_failure_rate"] == 0.0

    def test_error_safe(self):
        with patch("tools.skill_usage.get_record", side_effect=RuntimeError):
            rec = get_provenance_record("error")
        assert rec["invocation_count"] == 0
