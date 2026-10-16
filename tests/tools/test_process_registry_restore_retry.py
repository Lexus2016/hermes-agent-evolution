"""Retry failed launch-ledger replay without duplicate offers or profile leakage."""
import queue
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from tools import async_delegation as ledger
from tools.process_registry import ProcessRegistry


@pytest.fixture
def replay(tmp_path, monkeypatch):
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    user_home = tmp_path / "user-home"
    user_home.mkdir()
    monkeypatch.setenv("HOME", str(user_home))
    monkeypatch.setattr(Path, "home", lambda: user_home)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    token = set_hermes_home_override(None)
    monkeypatch.setattr(ledger, "_offered", set())
    registry = ProcessRegistry.__new__(ProcessRegistry)
    registry.completion_queue = queue.Queue()
    registry._completion_consumed = set()
    registry._poll_observed = set()
    try:
        yield registry, tmp_path
    finally:
        reset_hermes_home_override(token)


def _pending(identifier):
    now = time.time()
    ledger._persist_dispatch({"delegation_id": identifier, "goal": "synthetic", "context": None,
                              "toolsets": None, "role": "leaf", "model": "synthetic",
                              "session_key": "OWNER", "origin_ui_session_id": "",
                              "parent_session_id": "OWNER", "status": "running", "dispatched_at": now - 2})
    event = {"type": "async_delegation", "delegation_id": identifier, "session_key": "OWNER",
             "origin_ui_session_id": "", "goal": "synthetic", "status": "success",
             "summary": "synthetic", "completed_at": now - 1, "dispatched_at": now - 2}
    ledger._persist_completion(event, {"summary": "synthetic"})


def test_sqlite_lock_failure_retries_next_drain_and_restores_caller_scope(replay, monkeypatch, caplog):
    from hermes_cli import sqlite_util
    from hermes_constants import get_hermes_home, reset_hermes_home_override, set_hermes_home_override

    registry, home = replay
    _pending("retry")
    original = sqlite_util.open_db
    def short_wait(*args, **kwargs):
        kwargs["busy_timeout_ms"] = 20
        return original(*args, **kwargs)
    monkeypatch.setattr(sqlite_util, "open_db", short_wait)
    locked = sqlite3.connect(home / "state.db")
    locked.execute("PRAGMA journal_mode=DELETE")
    locked.execute("BEGIN EXCLUSIVE")
    secondary = home / "profiles" / "secondary"
    secondary.mkdir(parents=True)
    token = set_hermes_home_override(secondary)
    try:
        assert registry.drain_notifications("OWNER") == []
        assert get_hermes_home() == secondary
        assert "OperationalError" in caplog.text
        locked.rollback()
        results = registry.drain_notifications("OWNER")
        assert len(results) == 1
        assert results[0][0]["delegation_id"] == "retry"
        assert results[0][0]["restored"] is True
        assert get_hermes_home() == secondary
        assert registry.drain_notifications("OWNER") == []
        assert not (secondary / "state.db").exists()
    finally:
        locked.close()
        reset_hermes_home_override(token)


def test_partial_replay_failure_does_not_duplicate_already_offered_event(replay):
    registry, _ = replay
    _pending("first")
    _pending("second")
    class FailSecondPut(queue.Queue):
        calls = 0
        def put(self, item, *args, **kwargs):
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("synthetic queue interruption")
            return super().put(item, *args, **kwargs)
    registry.completion_queue = FailSecondPut()
    assert registry.restore_completions() == 0
    assert registry.restore_completions() == 1
    events = [registry.completion_queue.get_nowait() for _ in range(2)]
    assert {event["delegation_id"] for event in events} == {"first", "second"}
    assert registry.completion_queue.empty()
    assert registry.restore_completions() == 0


def test_inflight_and_reentrant_calls_skip_then_failure_allows_retry(replay, monkeypatch, caplog):
    registry, _ = replay
    entered, release = threading.Event(), threading.Event()
    calls = []
    def attempt(target):
        calls.append(target)
        assert registry.restore_completions() == 0
        if len(calls) == 1:
            entered.set()
            assert release.wait(3)
            raise RuntimeError("synthetic-private-failure")
        return 1
    monkeypatch.setattr(ledger, "restore_undelivered_completions", attempt)
    thread = threading.Thread(target=registry.restore_completions)
    thread.start()
    try:
        assert entered.wait(3)
        assert registry.restore_completions() == 0
    finally:
        release.set()
        thread.join(3)
    assert not thread.is_alive()
    assert registry.restore_completions() == 1
    assert registry.restore_completions() == 0
    assert len(calls) == 2
    assert "synthetic-private-failure" not in caplog.text


def test_missing_ledger_is_successfully_checked_only_once(replay, monkeypatch):
    registry, home = replay
    original = ledger.restore_undelivered_completions
    calls = []
    def attempt(target):
        calls.append(target)
        return original(target)
    monkeypatch.setattr(ledger, "restore_undelivered_completions", attempt)
    assert registry.restore_completions() == 0
    assert registry.restore_completions() == 0
    assert calls == [registry.completion_queue]
    assert not (home / "state.db").exists()
