"""Regression tests for #543 — introspection_extract resolves state.db correctly.

The canonical SessionDB lives at ``~/.hermes/state.db`` (or
``~/.hermes/profiles/<name>/state.db``). Legacy installs kept it inside
``sessions/state.db``. The build_digest resolver must pick the canonical path
first, and the per-session freshness check must enforce ``window_days`` on
DB-derived sessions.
"""

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from introspection_extract import build_digest  # noqa: E402


def _state_db(tmp_path, rows):
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT,
                tool_call_id TEXT,
                tool_calls TEXT,
                tool_name TEXT,
                timestamp REAL NOT NULL DEFAULT 0
            );
            """
        )
        for r in rows:
            params = (
                r["session_id"],
                r["role"],
                r.get("content"),
                r.get("tool_call_id"),
                json.dumps(r["tool_calls"]) if r.get("tool_calls") else None,
                r.get("tool_name"),
                r.get("timestamp", time.time()),
            )
            conn.execute(
                "INSERT INTO messages (session_id, role, content, tool_call_id, "
                "tool_calls, tool_name, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?)",
                params,
            )
        conn.commit()
    finally:
        conn.close()
    return db_path


def _asst(tool, cid):
    return {
        "role": "assistant",
        "tool_calls": [{"id": cid, "function": {"name": tool, "arguments": "{}"}}],
    }


def _tool(cid, content):
    return {"role": "tool", "tool_call_id": cid, "content": content}


def _term(exit_code=0, error=None):
    return json.dumps({"exit_code": exit_code, "error": error}, ensure_ascii=False)


class TestStateDbPathResolution:
    def test_sibling_state_db_is_scanned(self, tmp_path):
        # Modern layout: state.db is a sibling of sessions/
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        _state_db(
            tmp_path,  # state.db at tmp_path/state.db (sibling of sessions)
            [
                {"session_id": "s1", **_asst("terminal", "c1")},
                {
                    "session_id": "s1",
                    **_tool("c1", _term(exit_code=127, error="not found")),
                },
            ],
        )
        d = build_digest(sessions_dir, window_days=7)
        assert d["sessions_scanned"] == 1
        assert d["signals"]["tool_failures"] == {"terminal": 1}

    def test_legacy_in_sessions_state_db_still_works(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        _state_db(
            sessions_dir,
            [
                {"session_id": "s1", **_asst("read_file", "c1")},
                {"session_id": "s1", **_tool("c1", json.dumps({"error": "missing"}))},
            ],
        )
        d = build_digest(sessions_dir, window_days=7)
        assert d["sessions_scanned"] == 1
        assert d["signals"]["tool_failures"] == {"read_file": 1}

    def test_canonical_sibling_preferred_over_legacy(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        # Legacy in-sessions DB has one session
        _state_db(
            sessions_dir,
            [
                {"session_id": "legacy", **_asst("read_file", "c1")},
                {
                    "session_id": "legacy",
                    **_tool("c1", json.dumps({"error": "missing"})),
                },
            ],
        )
        # Canonical sibling DB has a different session
        _state_db(
            tmp_path,
            [
                {"session_id": "modern", **_asst("terminal", "c2")},
                {"session_id": "modern", **_tool("c2", _term(exit_code=1))},
            ],
        )
        d = build_digest(sessions_dir, window_days=7)
        assert d["sessions_scanned"] == 1
        # Must have picked the canonical sibling, not the legacy one.
        assert d["signals"]["tool_failures"] == {"terminal": 1}

    def test_window_days_filters_db_sessions(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        now = time.time()
        old = now - 30 * 86400
        _state_db(
            tmp_path,
            [
                {"session_id": "fresh", **_asst("terminal", "c1"), "timestamp": now},
                {
                    "session_id": "fresh",
                    **_tool("c1", _term(exit_code=1)),
                    "timestamp": now,
                },
                {"session_id": "stale", **_asst("read_file", "c2"), "timestamp": old},
                {
                    "session_id": "stale",
                    **_tool("c2", json.dumps({"error": "missing"})),
                    "timestamp": old,
                },
            ],
        )
        d = build_digest(sessions_dir, window_days=7, now=now)
        assert d["sessions_scanned"] == 1
        assert d["signals"]["tool_failures"] == {"terminal": 1}


class TestHermesHomeEnv:
    def test_hermes_home_env_resolves_state_db(self, tmp_path, monkeypatch):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        profile_dir = tmp_path / "profiles" / "user1"
        profile_dir.mkdir(parents=True)
        # sessions_dir points at tmp_path/sessions, but HERMES_HOME points at the
        # named profile dir, whose state.db should be consulted.
        real_state_db = profile_dir / "state.db"
        _state_db(
            profile_dir,
            [
                {"session_id": "s1", **_asst("terminal", "c1")},
                {"session_id": "s1", **_tool("c1", _term(exit_code=1))},
            ],
        )
        monkeypatch.setenv("HERMES_HOME", str(profile_dir))
        d = build_digest(sessions_dir, window_days=7)
        assert d["sessions_scanned"] == 1
        assert d["signals"]["tool_failures"] == {"terminal": 1}
