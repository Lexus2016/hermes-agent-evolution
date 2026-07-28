"""Tests for retrieval-utility logging + history-based deletion (issue #1480).

Uses stdlib + pytest + unittest.mock only. No live network calls.
Run via ``scripts/run_tests.sh tests/agent/test_retrieval_utility.py -q``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from agent.retrieval_utility import (
    clear_log,
    compute_utility,
    delete_low_utility_records,
    derive_outcome,
    load_log,
    record_outcome,
    record_retrieval,
    save_log,
)


@pytest.fixture
def isolated_utility_file(tmp_path, monkeypatch):
    """Redirect HERMES_HOME to a temp dir so tests don't touch the real sidecar."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    yield tmp_path
    # cleanup is automatic with tmp_path


class TestDeriveOutcome:
    """Test the friction-signal → outcome-label mapping."""

    def test_empty_signals_is_helpful(self):
        assert derive_outcome({}) == "helpful"

    def test_retries_only_is_neutral(self):
        assert derive_outcome({"retries": 2}) == "neutral"

    def test_task_failures_is_harmful(self):
        assert derive_outcome({"task_failures": 1}) == "harmful"

    def test_human_corrections_is_harmful(self):
        assert derive_outcome({"human_corrections": 1}) == "harmful"

    def test_task_failures_dominates_retries(self):
        assert derive_outcome({"retries": 5, "task_failures": 1}) == "harmful"

    def test_human_corrections_dominates_retries(self):
        assert derive_outcome({"retries": 3, "human_corrections": 1}) == "harmful"


class TestRecordAndRetrieve:
    """Test record_retrieval / record_outcome / compute_utility round-trip."""

    def test_empty_record_id_is_noop(self, isolated_utility_file):
        record_retrieval("", "context")
        log = load_log()
        assert log["retrievals"] == []

    def test_record_retrieval_creates_entry(self, isolated_utility_file):
        record_retrieval("memory:builtin", "user query about X", session_id="s1")
        log = load_log()
        assert len(log["retrievals"]) == 1
        entry = log["retrievals"][0]
        assert entry["record_id"] == "memory:builtin"
        assert entry["session_id"] == "s1"
        assert entry["outcome"] is None

    def test_query_text_is_not_stored(self, isolated_utility_file):
        """The context argument is accepted but never persisted.

        compute_utility scores outcomes only, so the query text was write-only
        — a fragment of the user's prompt on disk for no downstream purpose.
        """
        record_retrieval("memory:builtin", "user query about X", session_id="s1")
        entry = load_log()["retrievals"][0]
        assert "retrieval_context" not in entry
        assert "user query about X" not in json.dumps(entry)
        assert entry["session_id"] == "s1"
        assert entry["outcome"] is None
        assert "timestamp" in entry

    def test_record_outcome_fills_pending_entry(self, isolated_utility_file):
        record_retrieval("memory:builtin", "query", session_id="s1")
        record_outcome("memory:builtin", outcome="helpful", friction_signals={})
        log = load_log()
        assert log["retrievals"][0]["outcome"] == "helpful"
        assert log["retrievals"][0]["friction_signals"] == {}

    def test_record_outcome_skips_when_no_pending(self, isolated_utility_file):
        record_retrieval("memory:builtin", "query", session_id="s1")
        record_outcome("memory:builtin", outcome="helpful")
        # Second outcome should NOT create a duplicate or overwrite.
        record_outcome("memory:builtin", outcome="harmful")
        log = load_log()
        assert len(log["retrievals"]) == 1
        assert log["retrievals"][0]["outcome"] == "helpful"

    def test_compute_utility_returns_none_for_missing(self, isolated_utility_file):
        assert compute_utility("nonexistent") is None

    def test_compute_utility_all_helpful(self, isolated_utility_file):
        for _ in range(3):
            record_retrieval("memory:builtin", "q")
            record_outcome("memory:builtin", outcome="helpful", friction_signals={})
        result = compute_utility("memory:builtin")
        assert result is not None
        assert result["retrieval_count"] == 3
        assert result["avg_utility"] == 1.0
        assert result["outcomes"]["helpful"] == 3

    def test_compute_utility_mixed_outcomes(self, isolated_utility_file):
        record_retrieval("memory:builtin", "q")
        record_outcome("memory:builtin", outcome="helpful")
        record_retrieval("memory:builtin", "q")
        record_outcome("memory:builtin", outcome="harmful")
        record_retrieval("memory:builtin", "q")
        record_outcome("memory:builtin", outcome="neutral")
        result = compute_utility("memory:builtin")
        assert result is not None
        assert result["retrieval_count"] == 3
        # (1.0 + 0.0 + 0.5) / 3 = 0.5
        assert result["avg_utility"] == 0.5

    def test_compute_utility_unknown_outcomes_excluded_from_avg(
        self, isolated_utility_file
    ):
        record_retrieval("memory:builtin", "q")
        # No outcome recorded → unknown
        record_retrieval("memory:builtin", "q")
        record_outcome("memory:builtin", outcome="helpful")
        result = compute_utility("memory:builtin")
        assert result is not None
        assert result["retrieval_count"] == 2
        # Only 1 matched outcome (helpful=1.0), so avg = 1.0
        assert result["avg_utility"] == 1.0


class TestDeleteLowUtility:
    """Test history-based deletion eligibility."""

    def test_no_retrievals_returns_empty(self, isolated_utility_file):
        assert delete_low_utility_records() == []

    def test_below_min_retrievals_not_eligible(self, isolated_utility_file):
        for _ in range(2):
            record_retrieval("memory:builtin", "q")
            record_outcome("memory:builtin", outcome="harmful")
        # 2 retrievals < default min_retrievals=3
        assert delete_low_utility_records() == []

    def test_low_utility_record_is_eligible(self, isolated_utility_file):
        for _ in range(3):
            record_retrieval("memory:builtin", "q")
            record_outcome("memory:builtin", outcome="harmful")
        eligible = delete_low_utility_records()
        assert "memory:builtin" in eligible

    def test_high_utility_record_not_eligible(self, isolated_utility_file):
        for _ in range(3):
            record_retrieval("memory:builtin", "q")
            record_outcome("memory:builtin", outcome="helpful")
        eligible = delete_low_utility_records()
        assert "memory:builtin" not in eligible

    def test_custom_thresholds(self, isolated_utility_file):
        for _ in range(5):
            record_retrieval("memory:builtin", "q")
            record_outcome("memory:builtin", outcome="neutral")
        # neutral = 0.5 utility. With floor=0.6, neutral records are eligible.
        eligible = delete_low_utility_records(min_retrievals=4, utility_floor=0.6)
        assert "memory:builtin" in eligible
        # With floor=0.4, neutral records are NOT eligible.
        eligible = delete_low_utility_records(min_retrievals=4, utility_floor=0.4)
        assert "memory:builtin" not in eligible

    def test_does_not_mutate_log(self, isolated_utility_file):
        for _ in range(3):
            record_retrieval("memory:builtin", "q")
            record_outcome("memory:builtin", outcome="harmful")
        before = load_log()
        delete_low_utility_records()
        after = load_log()
        assert before == after


class TestLogPersistence:
    """Test load/save round-trip and corruption resilience."""

    def test_load_missing_returns_empty(self, isolated_utility_file):
        log = load_log()
        assert log["retrievals"] == []
        assert log["outcomes"] == {}

    def test_save_then_load_roundtrip(self, isolated_utility_file):
        data = {
            "retrievals": [{"record_id": "test", "outcome": "helpful"}],
            "outcomes": {},
        }
        save_log(data)
        loaded = load_log()
        assert loaded["retrievals"][0]["record_id"] == "test"

    def test_load_corrupt_returns_empty(self, isolated_utility_file):
        path = Path(os.environ["HERMES_HOME"]) / "memory" / ".retrieval_utility.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json{{", encoding="utf-8")
        log = load_log()
        assert log["retrievals"] == []

    def test_clear_log(self, isolated_utility_file):
        record_retrieval("memory:builtin", "q")
        record_outcome("memory:builtin", outcome="helpful")
        clear_log()
        log = load_log()
        assert log["retrievals"] == []

    def test_max_log_entries_cap(self, isolated_utility_file):
        # Write more than the cap to verify pruning.
        from agent.retrieval_utility import _MAX_LOG_ENTRIES

        for i in range(_MAX_LOG_ENTRIES + 100):
            record_retrieval(f"memory:{i}", "q")
        log = load_log()
        assert len(log["retrievals"]) == _MAX_LOG_ENTRIES
        # Oldest entries should be pruned (kept = most recent).
        ids = [r["record_id"] for r in log["retrievals"]]
        assert f"memory:0" not in ids
        assert f"memory:{_MAX_LOG_ENTRIES + 99}" in ids
