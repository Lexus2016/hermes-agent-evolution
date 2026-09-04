# -*- coding: utf-8 -*-
"""Message provenance: the ``origin`` column on ``messages``.

Phase 1a of the pinned-constraint plan. A security gate in
``agent/context_compressor.py`` needs to know whether a ``role="user"`` row
really came from a human, because the runtime relabels attacker-reachable
content as ``role="user"`` in several places (``gateway/wake.py`` self-posts
background-process stdout and delegation summaries, a compaction summary can be
emitted with ``role="user"``, and steer text is extracted out of tool results
into a bare user row). Four attempts to draw that boundary on the role LABEL
were each bypassed.

Two properties matter and are asserted here:

* **It round-trips.** ``compress()`` and every other consumer read the
  in-memory message dict, never the database row, so a column that does not
  make it back onto the dict is invisible to them — the exact mis-drawn
  boundary this replaces.
* **It fails closed.** Legacy rows carry NULL and anything unrecognised
  normalises to None. A reader must treat that as untrusted, so a missed
  runtime-authoring site is a false negative (safe), never a false positive.
"""

from __future__ import annotations

import re
import sqlite3

import pytest

from hermes_state import (
    MESSAGE_ORIGIN_API,
    MESSAGE_ORIGIN_HUMAN,
    MESSAGE_ORIGIN_RUNTIME,
    SessionDB,
)
from hermes_state_common import SCHEMA_SQL


@pytest.fixture()
def db(tmp_path) -> SessionDB:
    return SessionDB(str(tmp_path / "state.db"))


_SESSION_SEQ = iter(range(1_000_000))


def _session(db: SessionDB) -> str:
    # Distinct titles: two calls with the same title can resolve to one session,
    # which would make a source/target copy test compare a session with itself.
    return db.create_session(f"provenance test {next(_SESSION_SEQ)}", "cli")


def _origins(db: SessionDB, session_id: str):
    return [m.get("origin") for m in db.get_messages_as_conversation(session_id)]


class TestRoundTrip:
    """A column the in-memory dict never sees would protect nothing."""

    def test_append_message(self, db):
        sid = _session(db)
        db.append_message(sid, "user", content="a", origin=MESSAGE_ORIGIN_HUMAN)
        db.append_message(sid, "user", content="b", origin=MESSAGE_ORIGIN_RUNTIME)
        db.append_message(sid, "user", content="c", origin=MESSAGE_ORIGIN_API)
        assert _origins(db, sid) == ["human", "runtime", "api"]

    def test_replace_messages(self, db):
        """replace_messages goes through _insert_message_rows, whose fixed
        key list silently discards anything it does not name."""
        sid = _session(db)
        db.replace_messages(
            sid,
            [
                {"role": "user", "content": "a", "origin": MESSAGE_ORIGIN_HUMAN},
                {"role": "user", "content": "b", "origin": MESSAGE_ORIGIN_RUNTIME},
            ],
        )
        assert _origins(db, sid) == ["human", "runtime"]

    def test_append_messages_batch(self, db):
        sid = _session(db)
        db.append_messages_batch(
            sid,
            [
                {"role": "user", "content": "a", "origin": MESSAGE_ORIGIN_HUMAN},
                {"role": "user", "content": "b", "origin": MESSAGE_ORIGIN_RUNTIME},
            ],
        )
        assert _origins(db, sid) == ["human", "runtime"]

    def test_branch_shaped_copy_preserves_origin(self, db):
        """`/branch` rebuilds each row field by field; a copy of a human turn
        is still a human turn."""
        sid = _session(db)
        db.append_message(sid, "user", content="original", origin=MESSAGE_ORIGIN_HUMAN)
        source = db.get_messages_as_conversation(sid)
        target = _session(db)
        db.append_messages_batch(
            target,
            [
                {
                    "role": m.get("role", "user"),
                    "content": m.get("content"),
                    "origin": m.get("origin"),
                }
                for m in source
            ],
        )
        assert _origins(db, target) == ["human"]


class TestFailsClosed:
    """Unrecognised or absent provenance is untrusted, never human."""

    def test_legacy_row_has_no_origin(self, db):
        sid = _session(db)
        db.append_message(sid, "user", content="written before this existed")
        assert _origins(db, sid) == [None]

    @pytest.mark.parametrize(
        "forged",
        ["human!!", "HUMAN", "Human", " human", "human ", "", "trusted", "1"],
    )
    def test_forged_values_normalise_to_none(self, db, forged):
        sid = _session(db)
        db.append_message(sid, "user", content="x", origin=forged)
        assert _origins(db, sid) == [None], f"{forged!r} must not read as provenance"

    @pytest.mark.parametrize("forged", [None, 1, True, [], {}, object()])
    def test_non_string_values_normalise_to_none(self, db, forged):
        sid = _session(db)
        db.append_message(sid, "user", content="x", origin=forged)
        assert _origins(db, sid) == [None]

    def test_forged_value_through_the_dict_path(self, db):
        sid = _session(db)
        db.replace_messages(
            sid, [{"role": "user", "content": "x", "origin": "HUMAN"}]
        )
        assert _origins(db, sid) == [None]

    def test_normalizer_is_the_single_gate(self):
        from hermes_state import _normalize_message_origin

        assert _normalize_message_origin("human") == "human"
        assert _normalize_message_origin("nonsense") is None
        assert _normalize_message_origin(None) is None


class TestSchemaMigration:
    """Existing installs must gain the column without a hand-written migration."""

    def test_legacy_database_is_migrated_on_open(self, tmp_path):
        path = str(tmp_path / "legacy.db")
        legacy = re.sub(
            r",\n(?:\s*--[^\n]*\n)+\s*origin TEXT\n\);", "\n);", SCHEMA_SQL, count=1
        )
        messages_block = legacy.split("CREATE TABLE IF NOT EXISTS messages")[1].split(
            ");"
        )[0]
        assert "origin" not in messages_block, "fixture failed to strip the column"

        conn = sqlite3.connect(path)
        conn.executescript(legacy)
        conn.commit()
        conn.close()

        def columns():
            c = sqlite3.connect(path)
            try:
                return [r[1] for r in c.execute('PRAGMA table_info("messages")')]
            finally:
                c.close()

        assert "origin" not in columns()
        db = SessionDB(path)
        assert "origin" in columns(), "declarative column sync did not migrate"

        sid = _session(db)
        db.append_message(sid, "user", content="x", origin=MESSAGE_ORIGIN_HUMAN)
        assert _origins(db, sid) == ["human"]

    def test_no_check_constraint_blocks_legacy_null_rows(self, db):
        """A CHECK added via ALTER TABLE would also apply to later writes."""
        sid = _session(db)
        db.append_message(sid, "user", content="legacy-shaped")
        db.append_message(sid, "user", content="typed", origin=MESSAGE_ORIGIN_HUMAN)
        assert _origins(db, sid) == [None, "human"]


class TestForgeryPaths:
    """Provenance must not be assertable by anything outside the ingress set."""

    def test_import_cannot_assert_human_provenance(self, db):
        """`/api/sessions/import` takes arbitrary caller JSON; foreign history
        is foreign, so an imported `origin` is dropped rather than trusted."""
        db.import_sessions(
            [
                {
                    "id": "imported-1",
                    "title": "imported",
                    "source": "cli",
                    "messages": [
                        {
                            "role": "user",
                            "content": "attacker-chosen rule",
                            "origin": "human",
                        }
                    ],
                }
            ]
        )
        rows = [
            m
            for m in db.get_messages_as_conversation("imported-1")
            if m.get("content") == "attacker-chosen rule"
        ]
        assert rows, "the import did not land; the test would be vacuous"
        assert all(m.get("origin") is None for m in rows), (
            "imported data asserted human provenance"
        )

    def test_a_value_written_outside_the_api_is_not_trusted_on_read(self, db):
        """The column carries no CHECK, so reads normalise too."""
        import sqlite3

        sid = _session(db)
        db.append_message(sid, "user", content="x")
        conn = sqlite3.connect(db.db_path)
        try:
            conn.execute("UPDATE messages SET origin = ? WHERE session_id = ?",
                         ("Human", sid))
            conn.commit()
        finally:
            conn.close()
        assert _origins(db, sid) == [None], "an invalid stored value was trusted"

    def test_a_valid_value_written_outside_still_reads(self, db):
        """Normalisation must not break legitimate values written elsewhere."""
        import sqlite3

        sid = _session(db)
        db.append_message(sid, "user", content="x")
        conn = sqlite3.connect(db.db_path)
        try:
            conn.execute("UPDATE messages SET origin = ? WHERE session_id = ?",
                         ("human", sid))
            conn.commit()
        finally:
            conn.close()
        assert _origins(db, sid) == ["human"]

    def test_reasoning_shaped_copy_preserves_origin(self, db):
        """The TUI/CLI branch builders use a different field list to /branch."""
        sid = _session(db)
        db.append_message(sid, "user", content="orig", origin=MESSAGE_ORIGIN_HUMAN)
        source = db.get_messages_as_conversation(sid)
        target = _session(db)
        db.append_messages_batch(
            target,
            [
                {
                    "role": m.get("role", "user"),
                    "content": m.get("content"),
                    "origin": m.get("origin"),
                    "reasoning": m.get("reasoning"),
                }
                for m in source
            ],
        )
        assert _origins(db, target) == ["human"]


class TestImportTrustBoundary:
    """External imports reset provenance; an in-process move carries it."""

    _PAYLOAD = [
        {
            "id": "imported-trust",
            "title": "t",
            "source": "cli",
            "messages": [{"role": "user", "content": "x", "origin": "human"}],
        }
    ]

    def test_default_import_resets_origin(self, db):
        db.import_sessions([dict(s) for s in self._PAYLOAD])
        assert _origins(db, "imported-trust") == [None]

    def test_trusted_move_carries_origin(self, db):
        """Profile adoption moves our own rows; resetting them would
        silently downgrade a real human turn."""
        db.import_sessions([dict(s) for s in self._PAYLOAD], trust_origin=True)
        assert _origins(db, "imported-trust") == ["human"]

    def test_trust_is_keyword_only_and_defaults_to_false(self):
        import inspect

        sig = inspect.signature(SessionDB.import_sessions)
        param = sig.parameters["trust_origin"]
        assert param.kind is inspect.Parameter.KEYWORD_ONLY
        assert param.default is False
