"""Tests for agent/compaction_eval.py — CompactionRL eval signal."""

import json
from pathlib import Path
from unittest.mock import patch
from agent.compaction_eval import record_compaction_event, get_eval_summary


def test_record_and_summary(tmp_path):
    events_file = tmp_path / "logs" / "compaction_events.jsonl"
    with patch("agent.compaction_eval._events_file", return_value=events_file):
        record_compaction_event(
            messages_before=100, messages_after=50,
            tokens_before=10000, tokens_after=5000, success=True,
        )
        record_compaction_event(
            messages_before=80, messages_after=40,
            tokens_before=8000, tokens_after=3000, success=False,
        )
        summary = get_eval_summary()
        assert summary["total_events"] == 2
        assert summary["total_tokens_saved"] == 10000
        assert summary["success_rate"] == 0.5
        assert 0 < summary["avg_ratio"] < 1


def test_empty_summary(tmp_path):
    events_file = tmp_path / "nonexistent.jsonl"
    with patch("agent.compaction_eval._events_file", return_value=events_file):
        s = get_eval_summary()
        assert s["total_events"] == 0
        assert s["success_rate"] is None


def test_no_success_recorded(tmp_path):
    events_file = tmp_path / "logs" / "compaction_events.jsonl"
    with patch("agent.compaction_eval._events_file", return_value=events_file):
        record_compaction_event(
            messages_before=10, messages_after=5,
            tokens_before=1000, tokens_after=500,
        )
        s = get_eval_summary()
        assert s["total_events"] == 1
        assert s["success_rate"] is None
        assert s["total_tokens_saved"] == 500