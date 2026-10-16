"""Focused regression coverage split from the runtime integration suite."""

import contextlib

from tests.tui_gateway.test_tui_gateway_server import (
    _neuter_agent_prewarm_timer,
    _reap_leaked_notification_pollers,
    _session,
    os,
    pytest,
    reset_hermes_home_override,
    server,
    set_hermes_home_override,
    threading,
    time,
)

def test_prompt_submit_row_id_real_sessiondb_unknown_refuses_despite_ordinal(
    monkeypatch, tmp_path
):
    """#82959 fail-closed: unknown row_id + valid ordinal must not truncate
    real SessionDB (the mass-delete class when durable id cannot resolve).
    """
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "rowid-refuse.db")
    session_key = "real-db-row-refuse"
    db.create_session(session_key, "cli")
    msgs = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply 1"},
        {"role": "user", "content": "second"},
        {"role": "assistant", "content": "reply 2"},
    ]
    with db._lock:
        db._insert_message_rows(db._conn, session_key, msgs)
        db._conn.commit()

    live_history = [{"role": m["role"], "content": m["content"]} for m in msgs]
    sess = _session(history=list(live_history), session_key=session_key)
    sid = "real-db-row-refuse-sid"
    server._sessions[sid] = sess
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(
        server, "_start_agent_build", lambda *a, **k: pytest.fail("must not start a turn")
    )
    monkeypatch.setattr(
        server, "_start_inflight_turn", lambda *a, **k: pytest.fail("must not start a turn")
    )

    n_before = len(db.get_messages_as_conversation(session_key))
    try:
        resp = server.handle_request(
            {
                "id": "1",
                "method": "prompt.submit",
                "params": {
                    "session_id": sid,
                    "text": "stale durable id",
                    "truncate_before_row_id": 999_999_999,
                    "truncate_before_user_ordinal": 0,
                    "confirm_truncate": True,
                    "confirm_empty_truncate": True,
                },
            }
        )
        assert resp.get("error") is not None
        assert resp["error"]["code"] == 4018
        assert len(sess["history"]) == 4
        assert len(db.get_messages_as_conversation(session_key)) == n_before
    finally:
        server._sessions.pop(sid, None)

def test_prompt_submit_row_id_misaligned_memory_refuses_content_swap(
    monkeypatch, tmp_path
):
    """#82959 heal-path guard: equal-length but content-misaligned live memory
    must refuse (4018), not zip-stamp durable ids positionally and cut the
    wrong turn. Probe 4a from the PR #83202 review.
    """
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "rowid-misalign-content.db")
    session_key = "real-db-row-misalign-content"
    db.create_session(session_key, "cli")
    msgs = [
        {"role": "user", "content": "A"},
        {"role": "assistant", "content": "ra"},
        {"role": "user", "content": "B"},
        {"role": "assistant", "content": "rb"},
    ]
    with db._lock:
        db._insert_message_rows(db._conn, session_key, msgs)
        db._conn.commit()
    rid_b = msgs[2]["_row_id"]
    original_row_ids = [message["_row_id"] for message in msgs]

    # Same length + same role pattern, but content positions swapped: a
    # positional stamp would mark live "B" with durable A's row id and the
    # cut would keep the very turn the user rewound past.
    live_history = [
        {"role": "user", "content": "B"},
        {"role": "assistant", "content": "rb"},
        {"role": "user", "content": "A"},
        {"role": "assistant", "content": "ra"},
    ]
    sess = _session(history=list(live_history), session_key=session_key)
    sid = "misalign-content-sid"
    server._sessions[sid] = sess
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(
        server, "_start_agent_build", lambda *a, **k: pytest.fail("must not start a turn")
    )
    monkeypatch.setattr(
        server, "_start_inflight_turn", lambda *a, **k: pytest.fail("must not start a turn")
    )

    n_before = len(db.get_messages_as_conversation(session_key))
    try:
        resp = server.handle_request(
            {
                "id": "1",
                "method": "prompt.submit",
                "params": {
                    "session_id": sid,
                    "text": "rewind B",
                    "truncate_before_row_id": rid_b,
                    "rebind_survivor_row_ids": [*original_row_ids, 999_999],
                    "confirm_truncate": True,
                },
            }
        )
        assert resp.get("error") is not None
        assert resp["error"]["code"] == 4018
        # Fail-closed: nothing stamped onto the misaligned dicts, nothing cut.
        assert all("_row_id" not in m for m in sess["history"])
        assert len(sess["history"]) == 4
        assert len(db.get_messages_as_conversation(session_key)) == n_before
    finally:
        server._sessions.pop(sid, None)

def test_prompt_submit_row_id_misaligned_memory_role_shift_targets_real_turn(
    monkeypatch, tmp_path
):
    """#82959 heal-path guard: equal-length but role-misaligned live memory
    must not be positionally stamped. The content-verified DB fallback still
    resolves the REAL target turn in live order — the cut drops exactly the
    addressed user turn, never a positionally mis-aimed one. Probe 4b from
    the PR #83202 review.
    """
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "rowid-misalign-role.db")
    session_key = "real-db-row-misalign-role"
    db.create_session(session_key, "cli")
    msgs = [
        {"role": "user", "content": "A"},
        {"role": "assistant", "content": "ra"},
        {"role": "user", "content": "B"},
        {"role": "assistant", "content": "rb"},
    ]
    with db._lock:
        db._insert_message_rows(db._conn, session_key, msgs)
        db._conn.commit()
    rid_b = msgs[2]["_row_id"]
    original_row_ids = [message["_row_id"] for message in msgs]

    live_history = [
        {"role": "user", "content": "A"},
        {"role": "assistant", "content": "ra"},
        {"role": "assistant", "content": "rb"},
        {"role": "user", "content": "B"},
    ]
    sess = _session(history=list(live_history), session_key=session_key)
    sid = "misalign-role-sid"
    server._sessions[sid] = sess
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_start_agent_build", lambda *a, **k: None)
    monkeypatch.setattr(server, "_start_inflight_turn", lambda *a, **k: None)

    try:
        resp = server.handle_request(
            {
                "id": "1",
                "method": "prompt.submit",
                "params": {
                    "session_id": sid,
                    "text": "rewind B",
                    "truncate_before_row_id": rid_b,
                    "rebind_survivor_row_ids": [*original_row_ids, 999_999],
                    "confirm_truncate": True,
                },
            }
        )
        assert resp.get("error") is None, resp
        # The addressed turn ("B") is dropped exactly; earlier live turns
        # survive untouched. Before the guard, a positional zip-stamp put a
        # user-row id on an assistant dict and the pre-guard fallback cut at
        # a mis-aimed index. (Fresh _row_id stamps on survivors are expected —
        # replace_messages re-inserts and re-stamps the surviving dicts.)
        survivors = [(m["role"], m["content"]) for m in sess["history"]]
        assert survivors == [
            ("user", "A"),
            ("assistant", "ra"),
            ("assistant", "rb"),
        ]
        # The live list was too misaligned to bind old survivors to their new
        # physical rows safely. All requested IDs known to the pre-write active
        # transcript are therefore cleared; an unrelated archived/ancestor ID
        # remains absent from the bounded map and keeps its identity.
        assert resp["result"]["survivor_row_id_map"] == {
            str(row_id): None for row_id in original_row_ids
        }
    finally:
        server._sessions.pop(sid, None)

def test_prompt_submit_row_id_db_fallback_ordinal_mapping_verifies_content(
    monkeypatch,
):
    """#82959 db-fallback guard: when live/durable lengths differ, mapping the
    durable user-ordinal onto live indices must verify the mapped turn shows
    the durable target's content — a repaired user;user merge shifts ordinals
    and would otherwise cut an extra turn silently.
    """
    replaced = []

    # Durable transcript (repaired): the merge collapsed two user turns, so
    # durable user-ordinal 1 ("second") maps onto a DIFFERENT live user turn.
    durable_history = [
        {"_row_id": 601, "role": "user", "content": "first\nfollow-up"},
        {"_row_id": 603, "role": "assistant", "content": "reply 1"},
        {"_row_id": 604, "role": "user", "content": "second"},
        {"_row_id": 605, "role": "assistant", "content": "reply 2"},
    ]
    # Live memory (unrepaired, longer): user ordinal 1 here is "follow-up",
    # NOT "second".
    live_history = [
        {"role": "user", "content": "first"},
        {"role": "user", "content": "follow-up"},
        {"role": "assistant", "content": "reply 1"},
        {"role": "user", "content": "second"},
        {"role": "assistant", "content": "reply 2"},
    ]

    class _FakeDB:
        def replace_messages(
            self,
            key,
            messages,
            active_only=False,
            archive_dropped=False,
            reject_active_turn_lease=False,
        ):
            replaced.append((key, list(messages)))

        def get_messages_as_conversation(self, key, repair_alternation=False, include_row_ids=False):
            return [dict(m) for m in durable_history]

    sess = _session(history=list(live_history), session_key="db-fallback-verify-key")
    sid = "db-fallback-verify-sid"
    server._sessions[sid] = sess
    monkeypatch.setattr(server, "_get_db", lambda: _FakeDB())
    monkeypatch.setattr(
        server, "_start_agent_build", lambda *a, **k: pytest.fail("must not start a turn")
    )
    monkeypatch.setattr(
        server, "_start_inflight_turn", lambda *a, **k: pytest.fail("must not start a turn")
    )

    try:
        resp = server.handle_request(
            {
                "id": "1",
                "method": "prompt.submit",
                "params": {
                    "session_id": sid,
                    "text": "rewind to second",
                    "truncate_before_row_id": 604,
                    "confirm_truncate": True,
                },
            }
        )
        # Mapped live turn ("follow-up") does not match durable target
        # ("second") — refuse instead of cutting the wrong turn.
        assert resp.get("error") is not None
        assert resp["error"]["code"] == 4018
        assert replaced == []
        assert len(sess["history"]) == 5
    finally:
        server._sessions.pop(sid, None)

def _join_turn_thread(sess, timeout=10.0):
    """Join the real turn ``prompt.submit`` started, so nothing it does races the
    assertions or leaks into the next test. The submit's dispatch thread hands off to a
    ``prompt-turn-*`` worker that replaces ``_run_thread``: follow the handle until it
    stops changing."""
    deadline = time.monotonic() + timeout
    while isinstance(run_thread := sess.get("_run_thread"), threading.Thread):
        run_thread.join(timeout=max(0.0, deadline - time.monotonic()))
        assert not run_thread.is_alive(), "prompt.submit turn thread did not finish"
        if sess.get("_run_thread") is run_thread:
            return

@pytest.mark.parametrize("turn_isolation", [False, True])
def test_prompt_submit_consecutive_rewinds_with_returned_survivor_row_ids(
    monkeypatch, tmp_path, turn_isolation
):
    """#83202 review (consecutive-rewind staleness): replace_messages re-inserts
    the surviving prefix as NEW rows, so the pre-rewind client row ids die on
    the first rewind. The submit response must return the fresh survivor ids,
    and a second rewind using them must succeed where the stale id fail-closes.
    """
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "rowid-consec.db")
    session_key = "real-db-consec-rewind"
    db.create_session(session_key, "cli")
    msgs = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply 1"},
        {"role": "user", "content": "second"},
        {"role": "assistant", "content": "reply 2"},
        {"role": "user", "content": "third"},
        {"role": "assistant", "content": "reply 3"},
    ]
    with db._lock:
        db._insert_message_rows(db._conn, session_key, msgs)
        db._conn.commit()
    original_row_ids = [m["_row_id"] for m in msgs]

    sess = _session(history=[dict(m) for m in msgs], session_key=session_key)
    sid = "real-db-consec-rewind-sid"
    server._sessions[sid] = sess
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(
        server,
        "_load_cfg",
        lambda: {"dashboard": {"turn_isolation": turn_isolation}},
    )
    monkeypatch.setattr(
        server,
        "_submit_prompt_to_compute_host",
        lambda *_args, **_kwargs: server._ok("host", {"status": "streaming"}),
    )
    monkeypatch.setattr(server, "_start_agent_build", lambda *a, **k: None)
    monkeypatch.setattr(server, "_start_inflight_turn", lambda *a, **k: None)

    try:
        # Rewind 1: cut before "third" (last user turn). Survivors: turns
        # "first" + "second" (+ assistant replies) keep their rows (#82956).
        resp1 = server.handle_request(
            {
                "id": "1",
                "method": "prompt.submit",
                "params": {
                    "session_id": sid,
                    "text": "rewound third",
                    "truncate_before_row_id": original_row_ids[4],
                    "truncate_before_user_ordinal": 2,
                    "rebind_survivor_row_ids": [*original_row_ids, 999_999],
                    "confirm_truncate": True,
                },
            }
        )
        assert resp1.get("error") is None, resp1
        assert "survivor_user_row_ids" not in resp1["result"]
        row_id_map = resp1["result"].get("survivor_row_id_map")
        assert isinstance(row_id_map, dict)
        survivors = [
            row_id_map[str(original_row_ids[0])],
            row_id_map[str(original_row_ids[2])],
        ]
        # Only the dropped suffix is archived: the kept rows keep their ids and
        # the map says so; the two cut turns map to None.
        assert survivors == [original_row_ids[0], original_row_ids[2]]
        assert row_id_map == {
            **{str(rid): rid for rid in original_row_ids[:4]},
            str(original_row_ids[4]): None,
            str(original_row_ids[5]): None,
        }
        assert "999999" not in row_id_map
        # Wait for rewind 1's real turn thread to end (it clears ``running`` itself)
        # instead of forcing the flag: a still-live turn 1 overlapping rewind 2 is a
        # state production's ``running`` gate never allows, and its
        # _adopt_out_of_band_turns then read rewind 2's user row as a foreign turn.
        _join_turn_thread(sess)
        assert sess["running"] is False

        # Rewind 2: the id the client cached BEFORE rewind 1 is still the live row
        # (no 4018 refusal, no rebind dance) — the user-facing point of #82956.
        resp2 = server.handle_request(
            {
                "id": "2",
                "method": "prompt.submit",
                "params": {
                    "session_id": sid,
                    "text": "rewound second (same id)",
                    "truncate_before_row_id": original_row_ids[2],
                    "truncate_before_user_ordinal": 1,
                    "confirm_truncate": True,
                },
            }
        )
        assert resp2.get("error") is None, resp2
        _join_turn_thread(sess)
        assert len(sess["history"]) == 2
        assert sess["history"][0]["content"] == "first"
        active = db.get_messages_as_conversation(session_key)
        # The cut, plus the prompt just sent (durable at submit, #111868).
        assert [m["content"] for m in active] == ["first", "reply 1", "rewound second (same id)"]
        # And the second response rebinds again: one surviving user turn.
        survivors2 = resp2["result"].get("survivor_user_row_ids")
        assert isinstance(survivors2, list) and len(survivors2) == 1
    finally:
        server._sessions.pop(sid, None)

def test_prompt_submit_rebind_map_clears_active_row_hidden_by_sequence_repair(
    monkeypatch, tmp_path
):
    """The bounded map classifies physical active IDs before user;user repair."""
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "rowid-repaired-wedge.db")
    session_key = "real-db-rowid-repaired-wedge"
    db.create_session(session_key, "cli")
    physical = [
        {"role": "user", "content": "first fragment"},
        {"role": "user", "content": "second fragment"},
        {"role": "assistant", "content": "combined reply"},
        {"role": "user", "content": "target"},
        {"role": "assistant", "content": "target reply"},
    ]
    with db._lock:
        db._insert_message_rows(db._conn, session_key, physical)
        db._conn.commit()
    physical_ids = [message["_row_id"] for message in physical]
    repaired = db.get_messages_as_conversation(
        session_key, repair_alternation=True, include_row_ids=True
    )
    # Provider repair merges the wedge and necessarily drops the second
    # physical user's row identity from the replay view.
    assert physical_ids[1] not in {
        server._message_row_id(message) for message in repaired
    }

    sess = _session(
        history=[dict(message) for message in repaired], session_key=session_key
    )
    sid = "rowid-repaired-wedge-sid"
    server._sessions[sid] = sess
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_start_agent_build", lambda *a, **k: None)
    monkeypatch.setattr(server, "_start_inflight_turn", lambda *a, **k: None)

    try:
        response = server.handle_request(
            {
                "id": "1",
                "method": "prompt.submit",
                "params": {
                    "session_id": sid,
                    "text": "retry target",
                    "truncate_before_row_id": physical_ids[3],
                    "rebind_survivor_row_ids": [*physical_ids, 999_999],
                    "confirm_truncate": True,
                },
            }
        )
        assert response.get("error") is None, response
        row_id_map = response["result"]["survivor_row_id_map"]
        assert row_id_map[str(physical_ids[1])] is None
        assert "999999" not in row_id_map
    finally:
        server._sessions.pop(sid, None)

def test_prompt_submit_unconfirmed_truncation_refuses_before_target_resolution(
    monkeypatch,
):
    """Consent (4029) is checked BEFORE target resolution: an unconfirmed
    submit carrying truncation params must not pay the durable-transcript
    read or heal-stamp live history (simplify review on #83785), and an
    out-of-range unconfirmed ordinal refuses 4029, not 4018 — the baseline
    precedence before the row-id feature.
    """
    db_reads = []

    class _SpyDB:
        def get_messages_as_conversation(self, *a, **k):
            db_reads.append(1)
            return []

        def replace_messages(self, *a, **k):
            pytest.fail("must not write")

    hist = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "r1"},
        {"role": "user", "content": "second"},
        {"role": "assistant", "content": "r2"},
    ]
    sess = _session(history=list(hist), session_key="consent-precedence-key")
    sid = "consent-precedence-sid"
    server._sessions[sid] = sess
    monkeypatch.setattr(server, "_get_db", lambda: _SpyDB())
    monkeypatch.setattr(
        server, "_start_agent_build", lambda *a, **k: pytest.fail("must not start a turn")
    )
    monkeypatch.setattr(
        server, "_start_inflight_turn", lambda *a, **k: pytest.fail("must not start a turn")
    )

    try:
        # Unconfirmed row_id: 4029, no DB read, no heal stamps on history.
        resp = server.handle_request(
            {
                "id": "1",
                "method": "prompt.submit",
                "params": {"session_id": sid, "text": "x", "truncate_before_row_id": 3},
            }
        )
        assert (resp.get("error") or {}).get("code") == 4029, resp
        assert db_reads == []
        assert all("_row_id" not in m for m in sess["history"])

        # Malformed param still beats consent: bool row_id is 4004.
        resp = server.handle_request(
            {
                "id": "2",
                "method": "prompt.submit",
                "params": {"session_id": sid, "text": "x", "truncate_before_row_id": True},
            }
        )
        assert (resp.get("error") or {}).get("code") == 4004, resp

        # Unconfirmed out-of-range ordinal: 4029 (consent), not 4018 (range).
        resp = server.handle_request(
            {
                "id": "3",
                "method": "prompt.submit",
                "params": {
                    "session_id": sid,
                    "text": "x",
                    "truncate_before_user_ordinal": 99,
                },
            }
        )
        assert (resp.get("error") or {}).get("code") == 4029, resp
        assert len(sess["history"]) == 4
    finally:
        server._sessions.pop(sid, None)

def test_persist_live_session_system_prompt_uses_profile_home(monkeypatch, tmp_path):
    """Issue #50233: _persist_live_session_system_prompt must re-bind
    HERMES_HOME to the session's profile before rebuilding the system
    prompt.  Without this, a /model switch rebuilds the prompt with the
    root profile's SOUL.md and skills instead of the session's profile.
    """
    profile_home = tmp_path / "profile-work"
    profile_home.mkdir()
    (profile_home / "SOUL.md").write_text(
        "# Work persona\nYou are a work agent.", encoding="utf-8"
    )

    built_homes = []

    class FakeAgent:
        model = "test-model"
        provider = "test"
        _cached_system_prompt = None
        _session_db = None

        def _build_system_prompt(self, system_message=None):
            from hermes_constants import get_hermes_home
            home = get_hermes_home()
            built_homes.append(str(home))
            soul = (
                (home / "SOUL.md").read_text(encoding="utf-8-sig")
                if (home / "SOUL.md").exists()
                else ""
            )
            return f"System prompt from {home}\n{soul}"

    class FakeDB:
        def update_system_prompt(self, session_id, prompt):
            pass

    agent = FakeAgent()
    agent._session_db = FakeDB()
    session = {
        "agent": agent,
        "session_key": "test-key",
        "profile_home": str(profile_home),
    }

    server._persist_live_session_system_prompt(session)

    # The system prompt must have been built while the override pointed
    # to the profile home, not the root ~/.hermes.
    assert len(built_homes) == 1, f"expected 1 build, got {built_homes}"
    assert str(profile_home) in built_homes[0], (
        f"system prompt built with wrong home: {built_homes[0]}"
    )
    assert "Work persona" in agent._cached_system_prompt

    # The override must have been reset after the call.
    from hermes_constants import get_hermes_home_override
    assert get_hermes_home_override() is None

def test_persist_live_session_system_prompt_no_profile_is_unchanged(monkeypatch):
    """Sessions without a profile_home must not set/clear any override —
    the function should behave identically to before the fix."""
    class FakeAgent:
        model = "test"
        _cached_system_prompt = None
        _session_db = None

        def _build_system_prompt(self, system_message=None):
            return "plain prompt"

    class FakeDB:
        def update_system_prompt(self, session_id, prompt):
            pass

    agent = FakeAgent()
    agent._session_db = FakeDB()
    session = {
        "agent": agent,
        "session_key": "test-key",
        "profile_home": None,
    }

    # Should not raise, should still build and cache.
    server._persist_live_session_system_prompt(session)
    assert agent._cached_system_prompt == "plain prompt"

def test_persist_live_session_system_prompt_restores_pre_existing_override(tmp_path):
    """reset_hermes_home_override() restores the previous ContextVar state,
    not just the unset case: when a caller already holds an override, the
    persist call must scope to the session's profile and then hand the
    caller's override back, rather than clearing it to None."""
    from hermes_constants import get_hermes_home_override

    outer_home = tmp_path / "profile-outer"
    outer_home.mkdir()
    inner_home = tmp_path / "profile-inner"
    inner_home.mkdir()
    (inner_home / "SOUL.md").write_text(
        "# Inner persona\nYou are the inner agent.", encoding="utf-8"
    )

    built_homes = []

    class FakeAgent:
        model = "test-model"
        provider = "test"
        _cached_system_prompt = None
        _session_db = None

        def _build_system_prompt(self, system_message=None):
            from hermes_constants import get_hermes_home
            built_homes.append(str(get_hermes_home()))
            return "inner prompt"

    class FakeDB:
        def update_system_prompt(self, session_id, prompt):
            pass

    agent = FakeAgent()
    agent._session_db = FakeDB()
    session = {
        "agent": agent,
        "session_key": "test-key",
        "profile_home": str(inner_home),
    }

    outer_token = set_hermes_home_override(outer_home)
    try:
        server._persist_live_session_system_prompt(session)

        # The prompt was built under the session's profile, not the outer one.
        assert built_homes == [str(inner_home)]
        # The caller's pre-existing override survived, instead of being reset
        # to None.
        assert get_hermes_home_override() == str(outer_home)
    finally:
        reset_hermes_home_override(outer_token)
    assert get_hermes_home_override() is None

def test_persist_live_session_system_prompt_binds_session_cwd(monkeypatch, tmp_path):
    """The prompt rebuild after a live model switch must record the SESSION's
    working directory, not the process TERMINAL_CWD.

    The function runs on the RPC dispatcher thread (model.switch, config.set
    model). On that thread the _SESSION_CWD contextvar is not set, so
    resolve_agent_cwd() falls back to TERMINAL_CWD, which the desktop pins
    to the home directory. The wrong cwd line then persists into the stored
    prompt. Later turns restore the stored bytes without change (the
    prologue rebuilds only when _cached_system_prompt is None), so the
    poisoned line never self-heals.
    """
    session_cwd = tmp_path / "project"
    session_cwd.mkdir()
    process_cwd = tmp_path / "home-fallback"
    process_cwd.mkdir()
    monkeypatch.setenv("TERMINAL_CWD", str(process_cwd))

    persisted = {}

    class FakeAgent:
        model = "test-model"
        provider = "test"
        session_id = "cwd-test-session"
        _cached_system_prompt = None
        _session_db = None

        def _build_system_prompt(self, system_message=None):
            # The real builder embeds resolve_agent_cwd() via
            # prompt_builder.build_environment_hints().
            from agent.runtime_cwd import resolve_agent_cwd

            return f"Current working directory: {resolve_agent_cwd()}"

    class FakeDB:
        def update_system_prompt(self, session_id, prompt):
            persisted["prompt"] = prompt

    agent = FakeAgent()
    agent._session_db = FakeDB()
    session = {
        "agent": agent,
        "session_key": "cwd-test-session",
        "cwd": str(session_cwd),
        "explicit_cwd": True,
        "profile_home": None,
    }

    # A bare thread has no _SESSION_CWD contextvar — the RPC dispatcher shape.
    result = {}

    def dispatcher_thread():
        server._persist_live_session_system_prompt(session)
        result["cached"] = agent._cached_system_prompt

    t = threading.Thread(target=dispatcher_thread)
    t.start()
    t.join()

    expected = f"Current working directory: {session_cwd}"
    assert result["cached"] == expected, result["cached"]
    assert persisted["prompt"] == expected, persisted["prompt"]

def test_workspace_move_rehomes_running_session(monkeypatch, tmp_path):
    """An explicit Move-to-project must win for a RUNNING session: the stored
    row and the live runtime session re-anchor together, never a UI-vs-db
    disagreement (#86626)."""
    target = "stored-running-session"
    new_cwd = tmp_path / "dest-project"
    new_cwd.mkdir()
    captured = {}

    class FakeDB:
        def get_session(self, session_id):
            return {"id": session_id}

        def update_session_cwd(self, session_id, cwd, branch=None, root=None, replace_git_meta=True):
            captured["row_update"] = (session_id, cwd)

        def close(self):
            pass

    import contextlib

    @contextlib.contextmanager
    def _fake_db(_params, *, writer=False):
        yield FakeDB()

    monkeypatch.setattr(server, "_profile_db", _fake_db)
    monkeypatch.setattr(
        server.git_probe,
        "branch",
        lambda cwd: "main",
    )
    monkeypatch.setattr(
        server.git_probe,
        "common_repo_root",
        lambda cwd: str(new_cwd),
    )

    live = {"session_key": target, "running": True, "cwd": str(tmp_path / "old-project")}
    server._sessions["live-sid"] = live
    monkeypatch.setattr(server, "_register_session_cwd", lambda _session: None)

    res = server._methods["session.workspace.move"](
        "rid",
        {"session_key": target, "cwd": str(new_cwd)},
    )

    assert "error" not in res, res
    assert captured["row_update"] == (target, str(new_cwd))
    assert live["cwd"] == str(new_cwd)
    assert live.get("explicit_cwd") is True

def test_load_cfg_raw_sees_replacement_with_pinned_mtime_and_size(monkeypatch, tmp_path):
    """#111105: the raw-config cache must not serve (and later write back) a stale document after a
    same-size replacement that keeps the old mtime."""
    import shutil

    cfg = tmp_path / "config.yaml"
    cfg.write_text("model:\n  default: bbbb-route\n", encoding="utf-8")
    monkeypatch.setattr(server, "_active_config_path", lambda: cfg)
    monkeypatch.setattr(server, "_cfg_cache", None)
    monkeypatch.setattr(server, "_cfg_sig", None)
    monkeypatch.setattr(server, "_cfg_path", None)
    assert server._load_cfg_raw()["model"]["default"] == "bbbb-route"
    st = cfg.stat()
    other = tmp_path / "other.yaml"
    other.write_text("model:\n  default: aaaa-route\n", encoding="utf-8")
    # ctime ticks at the kernel's coarse clock (~4 ms): an in-place rewrite inside the tick of the
    # cached read leaves every stat field equal. Wait until the fs clock has passed that ctime.
    while other.stat().st_ctime_ns <= st.st_ctime_ns:
        os.utime(other)
    shutil.copy2(other, cfg)
    os.utime(cfg, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert server._load_cfg_raw()["model"]["default"] == "aaaa-route"

def test_session_create_uses_bound_profile_backend_not_launch(monkeypatch, tmp_path):
    """The regression: launch profile is LOCAL, the session is bound to an SSH profile.
    session.create must read the BOUND profile's backend, keep the remote cwd, and mark it
    explicit — not drop it to the launch dir."""
    remote = "/home/felix/projects/xsd-parser"
    assert not os.path.isdir(remote)
    # Launch process looks local; only the bound profile is ssh.
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.delenv("TERMINAL_CWD", raising=False)
    (tmp_path / "config.yaml").write_text("terminal:\n  backend: ssh\n", encoding="utf-8")
    monkeypatch.setattr(server, "_profile_home", lambda name: tmp_path if name == "felix" else None)
    monkeypatch.setattr(server, "_schedule_agent_build", lambda sid: None)
    monkeypatch.setattr(server, "_schedule_session_cap_enforcement", lambda: None)

    resp = server._methods["session.create"]("r1", {"cols": 80, "cwd": remote, "profile": "felix"})
    sid = resp["result"]["session_id"]
    session = server._sessions[sid]
    try:
        assert session["cwd"] == remote  # not dropped to getcwd()
        assert session["explicit_cwd"] is True
    finally:
        server._sessions.pop(sid, None)

def test_workspace_move_accepts_remote_dir_for_bound_ssh_profile(monkeypatch, tmp_path):
    """session.workspace.move must not reject a remote project dir that does not
    exist on the host when the bound profile is ssh (the 'working directory does
    not exist' error the user hit). Local profiles keep the isdir guard."""
    remote = "/home/felix/projects/xsd-parser"
    assert not os.path.isdir(remote)
    monkeypatch.setenv("TERMINAL_ENV", "local")  # launch looks local
    (tmp_path / "config.yaml").write_text("terminal:\n  backend: ssh\n", encoding="utf-8")
    monkeypatch.setattr(server, "_profile_home", lambda name: tmp_path if name == "felix" else None)

    class _DB:
        def get_session(self, key):
            return {"session_key": key}
        def update_session_cwd(self, *a, **k):
            return 1
    monkeypatch.setattr(server, "_profile_db", lambda params, **_kw: contextlib.nullcontext(_DB()))
    monkeypatch.setattr(server.git_probe, "branch", lambda c: None)
    monkeypatch.setattr(server.git_probe, "common_repo_root", lambda c: None)

    resp = server._methods["session.workspace.move"](
        "r1", {"session_key": "sk1", "cwd": remote, "profile": "felix"})
    assert "result" in resp, resp
    assert resp["result"]["cwd"] == remote

@pytest.mark.parametrize("launch_backend", ["ssh", "local"])
def test_ssh_named_profile_cwd_beats_launch_terminal_cwd(monkeypatch, tmp_path, launch_backend):
    """A named SSH profile's terminal.cwd is on the remote. The desktop must use
    it even when that path does not exist on this host and the process
    TERMINAL_CWD still names the launch profile."""
    remote = "/home/kali"
    launch = "/home/ubuntu/hermes_sync"
    assert not os.path.isdir(remote)
    home = tmp_path / "hunter"
    home.mkdir()
    (home / "config.yaml").write_text(
        "terminal:\n  backend: ssh\n  cwd: /home/kali\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TERMINAL_ENV", launch_backend)
    monkeypatch.setenv("TERMINAL_CWD", launch)
    monkeypatch.setattr(server, "_profile_home", lambda name: home if name == "hunter" else None)

    assert server._completion_cwd(
        {"profile": "hunter", "cwd": launch, "cwd_explicit": False}
    ) == remote
    assert server._terminal_task_cwd(
        {"cwd": launch, "explicit_cwd": False, "profile_home": str(home)}
    ) == remote
    assert server._terminal_task_cwd(
        {"cwd": "/opt/picked", "explicit_cwd": True, "profile_home": str(home)}
    ) == "/opt/picked"
    # A local launch profile must not "heal" the remote dir to its nearest host ancestor (/home).
    assert server._display_session_cwd({"cwd": remote, "profile_home": str(home)}) == remote
    # "~" names the REMOTE user's home, never this host's.
    (home / "config.yaml").write_text("terminal:\n  backend: ssh\n  cwd: ~/proj\n", encoding="utf-8")
    assert server._completion_cwd({"profile": "hunter", "cwd": launch, "cwd_explicit": False}) == "~/proj"
    # No terminal.cwd: the remote default is ~, never the launch profile's host cwd.
    (home / "config.yaml").write_text("terminal:\n  backend: ssh\n", encoding="utf-8")
    assert server._completion_cwd({"profile": "hunter"}) == "~"
    assert server._terminal_task_cwd({"cwd": launch, "explicit_cwd": False, "profile_home": str(home)}) == "~"

def test_named_profile_without_backend_stays_local_under_ssh_launch(monkeypatch, tmp_path):
    """A named profile that declares no terminal.backend is local; the launch profile's ssh backend must not
    turn its terminal.cwd into a remote path."""
    home = tmp_path / "plain"
    home.mkdir()
    (home / "config.yaml").write_text("terminal:\n  cwd: /srv/not-on-this-host\n", encoding="utf-8")
    launch = str(tmp_path)
    monkeypatch.setenv("TERMINAL_ENV", "ssh")
    monkeypatch.setattr(server, "_profile_home", lambda name: home if name == "plain" else None)

    assert server._completion_cwd({"profile": "plain", "cwd": launch, "cwd_explicit": False}) == launch
