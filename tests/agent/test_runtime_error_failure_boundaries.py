"""Diagnostic failures must preserve auth errors and fail-closed persistence."""

from pathlib import Path
import sqlite3
from unittest.mock import Mock

import pytest


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return home


def test_provider_catalog_failure_keeps_auth_error_envelope(monkeypatch):
    from agent.error_surface import build_error_surface_from_result
    from hermes_cli import provider_catalog

    catalog = Mock(side_effect=RuntimeError("synthetic optional provider catalog failure"))
    monkeypatch.setattr(provider_catalog, "provider_catalog_by_slug", catalog)
    result = {"failed": True, "error": "invalid API key", "failure_reason": "auth_permanent"}

    surface = build_error_surface_from_result(result, provider="openrouter", model="synthetic/model")

    assert surface["layer"] == "auth"
    assert surface["code"] == "auth_permanent"
    assert surface["retryable"] is False
    assert surface["provider"] == "openrouter"
    assert surface["model"] == "synthetic/model"
    assert surface["auth_kind"] == "api_key"
    assert "api_key_env" not in surface
    assert catalog.call_count >= 2  # auth kind and key-row enrichment both failed safely


@pytest.mark.parametrize("lookup_error", [RuntimeError, sqlite3.OperationalError])
def test_failed_session_lookup_does_not_recreate_or_replay_live_history(
    lookup_error, isolated_home, monkeypatch, caplog,
):
    from agent.context_compressor import _DB_PERSISTED_MARKER
    from agent.session_persistence import SessionPersistenceMixin
    from hermes_state import SessionDB

    db = SessionDB(db_path=isolated_home / "state.db")
    try:
        db.create_session("session-kept", source="cli")
        agent = SessionPersistenceMixin()
        agent.session_id = "session-kept"
        agent._session_db = db
        agent._session_db_created = True
        agent._ensure_db_session = Mock()
        history = [{"role": "user", "content": "saved question"}, {"role": "assistant", "content": "saved answer"}]
        assert agent._flush_messages_to_session_db(history, []) is True
        tail = {"role": "user", "content": "pending question"}
        messages = history + [tail]
        before = db.get_messages(agent.session_id)
        real_append = db.append_messages_batch
        real_get = db.get_session
        append = Mock(side_effect=sqlite3.IntegrityError("FOREIGN KEY constraint failed"))
        lookup = Mock(side_effect=lookup_error("synthetic row lookup failure"))
        monkeypatch.setattr(db, "append_messages_batch", append)
        monkeypatch.setattr(db, "get_session", lookup)

        assert agent._flush_messages_to_session_db(messages, history) is False

        append.assert_called_once()
        lookup.assert_called_once_with("session-kept")
        agent._ensure_db_session.assert_not_called()
        assert agent._session_db_created is True
        assert agent._last_persistence_error_cause == "session_row_missing"
        assert all(message[_DB_PERSISTED_MARKER] for message in history)
        assert not tail.get(_DB_PERSISTED_MARKER)
        assert db.get_messages(agent.session_id) == before
        assert "session row lookup failed" in caplog.text

        monkeypatch.setattr(db, "append_messages_batch", real_append)
        monkeypatch.setattr(db, "get_session", real_get)
        assert agent._flush_messages_to_session_db(messages, history) is True
        assert [row["content"] for row in db.get_messages(agent.session_id)] == [
            "saved question", "saved answer", "pending question",
        ]
        assert tail[_DB_PERSISTED_MARKER] is True
    finally:
        db.close()
