"""Tests for tools/delegate_session_stats.py (#3225)."""

from tools.delegate_session_stats import DELEGATE_SESSION_STATS, _DelegateSessionStats


def test_record_empty_results_is_noop():
    stats = _DelegateSessionStats()
    bucket = stats.record("sid", [])
    assert bucket == {"dispatches": 0, "completed": 0, "failed": 0}


def test_record_completed_and_failed_tasks():
    stats = _DelegateSessionStats()
    results = [
        {"status": "completed"},
        {"status": "completed"},
        {"status": "error", "error": "boom"},
    ]
    bucket = stats.record("sid", results)
    assert bucket == {"dispatches": 3, "completed": 2, "failed": 1}


def test_record_accumulates_across_calls():
    stats = _DelegateSessionStats()
    stats.record("sid", [{"status": "completed"}, {"status": "error"}])
    bucket = stats.record("sid", [{"status": "completed"}])
    assert bucket == {"dispatches": 3, "completed": 2, "failed": 1}


def test_get_snapshot_isolation():
    stats = _DelegateSessionStats()
    stats.record("sid", [{"status": "completed"}])
    snapshot = stats.get("sid")
    assert snapshot is not None
    snapshot["completed"] = 99
    assert stats.get("sid") == {"dispatches": 1, "completed": 1, "failed": 0}


def test_record_with_no_session_returns_none():
    stats = _DelegateSessionStats()
    assert stats.record(None, [{"status": "completed"}]) is None
    assert stats.record("", [{"status": "completed"}]) is None


def test_reset_session_clears_bucket():
    stats = _DelegateSessionStats()
    stats.record("sid", [{"status": "completed"}])
    assert stats.reset("sid") is True
    assert stats.get("sid") is None
    assert stats.reset("sid") is False


def test_global_instance_record_and_get():
    DELEGATE_SESSION_STATS.reset("global-test-sid")
    assert DELEGATE_SESSION_STATS.record(
        "global-test-sid", [{"status": "completed"}]
    ) == {
        "dispatches": 1,
        "completed": 1,
        "failed": 0,
    }
    assert DELEGATE_SESSION_STATS.get("global-test-sid") == {
        "dispatches": 1,
        "completed": 1,
        "failed": 0,
    }
    DELEGATE_SESSION_STATS.reset("global-test-sid")
