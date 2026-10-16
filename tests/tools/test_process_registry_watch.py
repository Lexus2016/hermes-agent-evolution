"""Watch delivery keeps the registry facade's runtime override and routing contracts."""

from types import SimpleNamespace

from tools import process_registry as pr


def test_watch_delivery_honors_facade_policy_clock_identity_and_redactor(monkeypatch):
    registry = pr.ProcessRegistry()
    session = pr.ProcessSession(
        id="proc_watch", command="worker", watch_patterns=["ready"],
        session_key="conversation", owner_task_id="owner", watcher_chat_id="chat",
    )
    monkeypatch.setattr(pr, "time", SimpleNamespace(time=lambda: 100.0))
    monkeypatch.setattr(pr, "uuid", SimpleNamespace(uuid4=lambda: SimpleNamespace(hex="event-1")))
    monkeypatch.setattr(pr, "WATCH_MIN_INTERVAL_SECONDS", 5)
    monkeypatch.setattr(pr, "WATCH_STRIKE_LIMIT", 1)
    monkeypatch.setattr(pr, "_redact_process_result", lambda result: result.update(output="redacted"))

    registry._check_watch_patterns(session, "ready: first\n")
    event = registry.completion_queue.get_nowait()

    assert event["type"] == "watch_match"
    assert event["output"] == "redacted"
    assert event["event_id"] == "event-1"
    assert event["session_key"] == "conversation"
    assert event["owner_task_id"] == "owner"
    assert event["chat_id"] == "chat"
    assert session._watch_cooldown_until == 105.0

    registry._check_watch_patterns(session, "ready: repeated\n")
    disabled = registry.completion_queue.get_nowait()
    assert disabled["type"] == "watch_disabled"
    assert disabled["suppressed"] == 1
    assert session._watch_disabled
    assert session.notify_on_complete
    assert registry.completion_queue.empty()


def test_global_watch_breaker_honors_facade_limits_and_releases_once(monkeypatch):
    registry = pr.ProcessRegistry()
    monkeypatch.setattr(pr, "WATCH_GLOBAL_MAX_PER_WINDOW", 1)
    monkeypatch.setattr(pr, "WATCH_GLOBAL_WINDOW_SECONDS", 10)
    monkeypatch.setattr(pr, "WATCH_GLOBAL_COOLDOWN_SECONDS", 3)

    assert registry._global_watch_admit(100.0)
    assert not registry._global_watch_admit(101.0)
    assert not registry._global_watch_admit(102.0)
    tripped = registry.completion_queue.get_nowait()
    assert tripped["type"] == "watch_overflow_tripped"
    assert registry.completion_queue.empty()

    assert registry._global_watch_admit(104.0)
    released = registry.completion_queue.get_nowait()
    assert released["type"] == "watch_overflow_released"
    assert released["suppressed"] == 2
    assert registry.completion_queue.empty()
