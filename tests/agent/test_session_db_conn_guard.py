"""Tests for issue #82 — session-DB connection guard + JSONL spool fallback.

Verifies that ``_flush_messages_to_session_db`` no longer silently drops a
turn's messages when the SessionDB handle is missing or its SQLite connection
has been closed/reaped underneath it:

1. ``_ensure_session_db_usable`` returns True and keeps a live handle.
2. A reaped connection (``_conn is None``) is re-opened lazily.
3. A genuinely unopenable store degrades to a recoverable JSONL spool instead
   of silent loss.
4. The spool only writes messages that never received the persist marker.
5. Persist-disabled forks never spool (they must not write to canonical home).
"""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from hermes_state import SessionDB


def _make_agent(session_db):
    """Create a minimal AIAgent with a (possibly None) session DB handle."""
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
        from run_agent import AIAgent

        return AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            model="test/model",
            quiet_mode=True,
            session_db=session_db,
            session_id="test-session-82",
            skip_context_files=True,
            skip_memory=True,
        )


class TestEnsureSessionDbUsable:
    def test_live_handle_returns_true_and_is_kept(self, tmp_path):
        db = SessionDB(db_path=tmp_path / "state.db")
        try:
            agent = _make_agent(db)
            assert agent._ensure_session_db_usable() is True
            assert agent._session_db is db
        finally:
            db.close()

    def test_persist_disabled_returns_false(self, tmp_path):
        db = SessionDB(db_path=tmp_path / "state.db")
        try:
            agent = _make_agent(db)
            agent._persist_disabled = True
            assert agent._ensure_session_db_usable() is False
        finally:
            db.close()

    def test_reaped_connection_is_reopened(self, tmp_path, monkeypatch):
        db = SessionDB(db_path=tmp_path / "state.db")
        db.create_session("test-session-82", source="test")
        agent = _make_agent(db)
        # Simulate the connection being reaped underneath the live handle.
        db.close()
        assert getattr(db, "_conn", None) is None

        # Redirect the lazy re-open to a temp path instead of canonical home.
        from hermes_state import SessionDB as RealSessionDB

        monkeypatch.setattr(
            "hermes_state.SessionDB",
            lambda *a, **k: RealSessionDB(db_path=tmp_path / "reopened.db"),
        )
        assert agent._ensure_session_db_usable() is True
        assert agent._session_db is not db
        assert getattr(agent._session_db, "_conn", None) is not None

    def test_unopenable_store_returns_false(self, tmp_path, monkeypatch):
        agent = _make_agent(None)

        def _boom(*a, **k):
            raise RuntimeError("state.db cannot be opened")

        monkeypatch.setattr("hermes_state.SessionDB", _boom)
        assert agent._ensure_session_db_usable() is False


class TestSpoolFallback:
    def test_flush_spools_when_store_unopenable(self, tmp_path, monkeypatch):
        agent = _make_agent(None)

        def _boom(*a, **k):
            raise RuntimeError("state.db cannot be opened")

        monkeypatch.setattr("hermes_state.SessionDB", _boom)
        monkeypatch.setattr(
            "hermes_constants.get_hermes_home", lambda: Path(tmp_path)
        )

        messages = [{"role": "user", "content": "spool me"}]
        # The flush must not raise, and must degrade to the JSONL spool.
        result = agent._flush_messages_to_session_db(messages, [])
        assert result is None

        spool_path = tmp_path / "sessions" / "spool-test-session-82.jsonl"
        assert spool_path.exists()
        lines = spool_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["content"] == "spool me"

    def test_spool_skips_already_persisted_messages(self, tmp_path, monkeypatch):
        agent = _make_agent(None)
        monkeypatch.setattr(
            "hermes_constants.get_hermes_home", lambda: Path(tmp_path)
        )

        from run_agent import _DB_PERSISTED_MARKER

        messages = [
            {"role": "user", "content": "new", _DB_PERSISTED_MARKER: False},
            {"role": "assistant", "content": "old", _DB_PERSISTED_MARKER: True},
        ]
        agent._spool_unpersisted_messages_to_jsonl(messages)

        spool_path = tmp_path / "sessions" / "spool-test-session-82.jsonl"
        lines = spool_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["content"] == "new"

    def test_spool_is_append_only_and_never_raises(self, tmp_path, monkeypatch):
        agent = _make_agent(None)
        monkeypatch.setattr(
            "hermes_constants.get_hermes_home", lambda: Path(tmp_path)
        )
        agent._spool_unpersisted_messages_to_jsonl(
            [{"role": "user", "content": "first"}]
        )
        agent._spool_unpersisted_messages_to_jsonl(
            [{"role": "user", "content": "second"}]
        )
        spool_path = tmp_path / "sessions" / "spool-test-session-82.jsonl"
        lines = spool_path.read_text(encoding="utf-8").splitlines()
        assert [json.loads(ln)["content"] for ln in lines] == ["first", "second"]

    def test_persist_disabled_never_spools(self, tmp_path, monkeypatch):
        db = SessionDB(db_path=tmp_path / "state.db")
        agent = _make_agent(db)
        agent._persist_disabled = True
        monkeypatch.setattr(
            "hermes_constants.get_hermes_home", lambda: Path(tmp_path)
        )
        result = agent._flush_messages_to_session_db(
            [{"role": "user", "content": "must not persist"}], []
        )
        assert result is None
        spool_path = tmp_path / "sessions" / "spool-test-session-82.jsonl"
        assert not spool_path.exists()
        db.close()
