"""Tests for scripts/introspection_extract.py — deterministic, anonymized digest (#89).

Tool-failure detection is STRUCTURAL (#347): every Hermes tool serialises its
result as a JSON envelope carrying the authoritative status (``exit_code`` for
terminal/code-exec, ``error``/``success``/``status`` for the rest). The digest
reads that status instead of substring-scanning the body, so marker words
("404", "error:", "failed") inside a SUCCESSFUL result's output no longer count
as failures. Fixtures therefore use realistic envelopes, not bare strings.
"""

import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from introspection_extract import build_digest, scan_session  # noqa: E402


def _session(tmp_path, name, lines, *, age_days=0):
    p = tmp_path / f"{name}.jsonl"
    p.write_text("\n".join(json.dumps(o) for o in lines) + "\n", encoding="utf-8")
    if age_days:
        old = time.time() - age_days * 86400
        import os

        os.utime(p, (old, old))
    return p


def _asst(tool, cid):
    return {
        "role": "assistant",
        "tool_calls": [{"id": cid, "function": {"name": tool, "arguments": "{}"}}],
    }


def _tool(cid, content):
    return {"role": "tool", "tool_call_id": cid, "content": content}


# --- realistic tool-result envelopes (#347) ----------------------------------
def _term(output="", *, exit_code=0, error=None):
    """Terminal / code-exec envelope: failure is signalled by exit_code != 0."""
    return json.dumps(
        {"output": output, "exit_code": exit_code, "error": error}, ensure_ascii=False
    )


def _ok(**fields):
    """A successful non-terminal envelope (e.g. read_file → {"content": ...}).

    No ``error``, no nonzero ``exit_code`` → never counted as a failure, even
    when ``fields`` carry marker words in their values."""
    return json.dumps(fields or {"success": True}, ensure_ascii=False)


def _fail(error="error"):
    """A failed non-terminal envelope (read_file/skill/etc. → {"error": ...})."""
    return json.dumps({"error": error}, ensure_ascii=False)


class TestScanSession:
    def test_attributes_failures_to_tool(self, tmp_path):
        p = _session(
            tmp_path,
            "s1",
            [
                {"role": "session_meta"},
                _asst("terminal", "c1"),
                _tool("c1", _term("bash: foo: command not found", exit_code=127)),
                _asst("terminal", "c2"),
                _tool("c2", _term("", exit_code=1, error="permission denied")),
                _asst("read_file", "c3"),
                _tool("c3", _ok(content="ok, file contents here")),
            ],
        )
        s = scan_session(p)
        assert s["tool_failures"] == {"terminal": 2}
        assert "read_file" not in s["tool_failures"]

    def test_structural_ignores_marker_words_in_successful_output(self, tmp_path):
        """#347 regression: marker words in the BODY of a SUCCESSFUL result
        must NOT be counted. The old substring matcher fired on file content
        ("HTTP 404"), grep stdout ("error:"), and skill docs ("timeout") even
        though every call succeeded; the structural classifier counts none."""
        p = _session(
            tmp_path,
            "fp",
            [
                _asst("read_file", "c1"),
                _tool("c1", _ok(content="page says HTTP 404 Not Found; error: none")),
                _asst("terminal", "c2"),
                _tool(
                    "c2",
                    _term("grep hit: error: deprecated\nbuild failed? no", exit_code=0),
                ),
                _asst("skill_view", "c3"),
                _tool("c3", _ok(content="docs cover 404 and timeout handling")),
            ],
        )
        s = scan_session(p)
        assert s["tool_failures"] == {}

    def test_error_field_counts_for_non_terminal_tools(self, tmp_path):
        p = _session(
            tmp_path,
            "ef",
            [
                _asst("read_file", "c1"),
                _tool("c1", _fail("no such file or directory")),
                _asst("patch", "c2"),
                _tool("c2", _ok(success=False)),
            ],
        )
        s = scan_session(p)
        assert s["tool_failures"] == {"read_file": 1, "patch": 1}

    def test_counts_timeouts_and_refusals(self, tmp_path):
        p = _session(
            tmp_path,
            "s2",
            [
                _asst("mcp_health", "c1"),
                _tool(
                    "c1", _term("", exit_code=-1, error="request timed out after 120s")
                ),
                {"role": "assistant", "content": "I can't access that path."},
            ],
        )
        s = scan_session(p)
        assert s["timeouts"] == 1
        assert s["refusals"] == 1

    def test_timeout_not_counted_when_tool_succeeded(self, tmp_path):
        """#400 regression: successful read_file whose content mentions "timeout"
        must NOT increment timeouts."""
        p = _session(
            tmp_path,
            "timeout_fp",
            [
                _asst("read_file", "c1"),
                _tool(
                    "c1",
                    _ok(content="docs cover timeout handling; timed out retry logic"),
                ),
            ],
        )
        s = scan_session(p)
        assert s["timeouts"] == 0
        assert s["tool_failures"] == {}

    def test_timeout_counted_when_tool_failed(self, tmp_path):
        """#400: a failed terminal result whose error says "timed out after 120s"
        DOES increment timeouts."""
        p = _session(
            tmp_path,
            "timeout_fail",
            [
                _asst("terminal", "c1"),
                _tool("c1", _term("", exit_code=1, error="timed out after 120s")),
            ],
        )
        s = scan_session(p)
        assert s["timeouts"] == 1
        assert s["tool_failures"] == {"terminal": 1}

    def test_repeated_run_detected(self, tmp_path):
        lines = [{"role": "session_meta"}]
        for i in range(6):
            lines += [_asst("terminal", f"c{i}"), _tool(f"c{i}", _term("ok"))]
        p = _session(tmp_path, "s3", lines)
        s = scan_session(p)
        assert s["repeated_tool_runs"].get("terminal") == 6

    def test_no_raw_text_in_output(self, tmp_path):
        secret = "USER SECRET email <REDACTED:email:db677acc382bd26bb3a00162f3e668d3> lives at 5 Main St"
        p = _session(
            tmp_path,
            "s4",
            [
                _asst("terminal", "c1"),
                _tool("c1", _term("", exit_code=1, error=secret)),
            ],
        )
        s = scan_session(p)
        # A genuine failure is counted, but the digest carries only counts/tool
        # names — never the raw content/error text.
        assert s["tool_failures"] == {"terminal": 1}
        assert secret not in json.dumps(s)


class TestFailureReasonClassification:
    """#1325 — failures are sub-classified by reason so the introspection loop
    attributes regressions to the actual mode, not just the tool name. Stops the
    fix→regress→refile treadmill where a fix for read_file:file-not-found is
    credited/blamed for read_file:timeout."""

    def test_file_not_found_reason(self, tmp_path):
        p = _session(
            tmp_path,
            "r1",
            [
                _asst("read_file", "c1"),
                _tool("c1", _fail("no such file or directory")),
            ],
        )
        s = scan_session(p)
        assert s["tool_failures"] == {"read_file": 1}
        assert s["tool_failures_by_reason"] == {"read_file": {"file-not-found": 1}}

    def test_permission_denied_reason(self, tmp_path):
        p = _session(
            tmp_path,
            "r2",
            [
                _asst("terminal", "c1"),
                _tool(
                    "c1", _term("cannot write", exit_code=1, error="permission denied")
                ),
            ],
        )
        s = scan_session(p)
        assert s["tool_failures_by_reason"] == {"terminal": {"permission-denied": 1}}

    def test_timeout_reason(self, tmp_path):
        p = _session(
            tmp_path,
            "r3",
            [
                _asst("terminal", "c1"),
                _tool("c1", _term("command timed out after 30s", exit_code=124)),
            ],
        )
        s = scan_session(p)
        # exit_code=124 → failed; "timed out" in output → timeout reason
        assert s["tool_failures_by_reason"] == {"terminal": {"timeout": 1}}

    def test_non_zero_exit_fallback_reason(self, tmp_path):
        """A failed terminal call with no recognizable keyword falls back to
        non-zero-exit (not 'other') because exit_code is the authoritative
        signal."""
        p = _session(
            tmp_path,
            "r4",
            [
                _asst("terminal", "c1"),
                _tool("c1", _term("just some output", exit_code=2)),
            ],
        )
        s = scan_session(p)
        assert s["tool_failures_by_reason"] == {"terminal": {"non-zero-exit": 1}}

    def test_other_reason_for_bare_error(self, tmp_path):
        """A non-terminal failure with a generic error message and no keyword
        match lands in 'other'."""
        p = _session(
            tmp_path,
            "r5",
            [
                _asst("patch", "c1"),
                _tool("c1", _fail("something unusual happened")),
            ],
        )
        s = scan_session(p)
        assert s["tool_failures_by_reason"] == {"patch": {"other": 1}}

    def test_no_reason_for_successful_results(self, tmp_path):
        p = _session(
            tmp_path,
            "r6",
            [
                _asst("read_file", "c1"),
                _tool("c1", _ok(content="page mentions 404 but call succeeded")),
            ],
        )
        s = scan_session(p)
        assert s["tool_failures"] == {}
        assert s["tool_failures_by_reason"] == {}

    def test_distinct_reasons_for_same_tool(self, tmp_path):
        """The core #1325 invariant: two failures of the same tool but with
        different reasons are counted separately, so a fix for one mode is not
        credited for the other."""
        p = _session(
            tmp_path,
            "r7",
            [
                _asst("read_file", "c1"),
                _tool("c1", _fail("no such file or directory")),
                _asst("read_file", "c2"),
                _tool("c2", _fail("operation timed out")),
            ],
        )
        s = scan_session(p)
        assert s["tool_failures"] == {"read_file": 2}
        assert s["tool_failures_by_reason"] == {
            "read_file": {"file-not-found": 1, "timeout": 1}
        }

    def test_digest_aggregates_reasons_across_sessions(self, tmp_path):
        """build_digest merges per-session reason counters into a window total."""
        _session(
            tmp_path,
            "s_a",
            [
                _asst("read_file", "c1"),
                _tool("c1", _fail("no such file or directory")),
            ],
        )
        _session(
            tmp_path,
            "s_b",
            [
                _asst("read_file", "c1"),
                _tool("c1", _fail("permission denied")),
                _asst("read_file", "c2"),
                _tool("c2", _fail("no such file or directory")),
            ],
        )
        d = build_digest(tmp_path)
        assert d["signals"]["tool_failures"] == {"read_file": 3}
        assert d["signals"]["tool_failures_by_reason"] == {
            "read_file": {"file-not-found": 2, "permission-denied": 1}
        }

    def test_no_raw_text_in_reason_breakdown(self, tmp_path):
        """The reason classifier reads only structured fields; raw user content
        must never leak into the digest."""
        secret = "USER SECRET token <REDACTED:token:abc> in body"
        p = _session(
            tmp_path,
            "r8",
            [
                _asst("terminal", "c1"),
                _tool("c1", _term(secret, exit_code=1, error="non-zero exit")),
            ],
        )
        s = scan_session(p)
        assert s["tool_failures_by_reason"] == {"terminal": {"non-zero-exit": 1}}
        assert secret not in json.dumps(s)


class TestBuildDigest:
    def test_window_excludes_old_sessions(self, tmp_path):
        _session(
            tmp_path,
            "recent",
            [_asst("terminal", "c1"), _tool("c1", _term(exit_code=127))],
        )
        _session(
            tmp_path,
            "old",
            [_asst("terminal", "c2"), _tool("c2", _term(exit_code=127))],
            age_days=30,
        )
        d = build_digest(tmp_path, window_days=7)
        assert d["sessions_scanned"] == 1
        assert d["signals"]["tool_failures"] == {"terminal": 1}

    def test_aggregates_across_sessions(self, tmp_path):
        for n in ("a", "b"):
            lines = [{"role": "session_meta"}]
            for i in range(5):
                lines += [_asst("terminal", f"{n}{i}"), _tool(f"{n}{i}", _term("ok"))]
            _session(tmp_path, n, lines)
        d = build_digest(tmp_path, window_days=7)
        assert d["sessions_scanned"] == 2
        rr = d["signals"]["repeated_tool_runs"]["terminal"]
        assert rr["sessions"] == 2 and rr["max_consecutive"] == 5

    def test_missing_dir_is_empty(self, tmp_path):
        d = build_digest(tmp_path / "nope", window_days=7)
        assert d["sessions_scanned"] == 0


def _dump(
    tmp_path, name, messages, *, session_id, model="glm-5.2", error=None, age_days=0
):
    obj = {
        "timestamp": "2026-06-16T00:00:00",
        "session_id": session_id,
        "reason": "error",
        "request": {
            "method": "POST",
            "url": "https://x/api",
            "headers": {},
            "body": {"model": model, "messages": messages, "tools": []},
        },
    }
    if error is not None:
        obj["error"] = error
    p = tmp_path / f"request_dump_{name}.json"
    p.write_text(json.dumps(obj), encoding="utf-8")
    if age_days:
        old = time.time() - age_days * 86400
        import os

        os.utime(p, (old, old))
    return p


class TestRequestDump:
    """#238 — installs that persist sessions as request_dump_*.json must be
    scanned too, else introspection reports zero signals and goes blind."""

    def test_scanned_when_no_jsonl_present(self, tmp_path):
        # The exact regression: a dir with only request dumps, no *.jsonl.
        _dump(
            tmp_path,
            "d1",
            [
                _asst("terminal", "c1"),
                _tool("c1", _term("bash: foo: command not found", exit_code=127)),
            ],
            session_id="sess-1",
            error={
                "type": "overloaded_error",
                "status_code": 529,
                "message": "x",
                "response_text": "y",
            },
        )
        d = build_digest(tmp_path, window_days=7)
        assert d["sessions_scanned"] == 1
        assert d["signals"]["tool_failures"] == {"terminal": 1}
        assert d["signals"]["provider_errors"] == {"529:overloaded_error": 1}
        assert d["signals"]["models_used"] == {"glm-5.2": 1}

    def test_dedup_by_session_keeps_most_complete(self, tmp_path):
        # Two dumps of ONE session (growing prefix) count once, via the larger.
        short = [
            _asst("terminal", "c1"),
            _tool("c1", _term("", exit_code=1, error="permission denied")),
        ]
        full = short + [
            _asst("terminal", "c2"),
            _tool("c2", _term("bash: x: command not found", exit_code=127)),
        ]
        _dump(tmp_path, "early", short, session_id="sess-1")
        _dump(tmp_path, "late", full, session_id="sess-1")
        d = build_digest(tmp_path, window_days=7)
        assert d["sessions_scanned"] == 1  # one session, not two dumps
        assert d["signals"]["tool_failures"] == {"terminal": 2}  # from the full one

    def test_mixed_jsonl_and_dump_both_counted(self, tmp_path):
        _session(
            tmp_path, "s1", [_asst("terminal", "c1"), _tool("c1", _term(exit_code=127))]
        )
        _dump(
            tmp_path,
            "d1",
            [_asst("read_file", "c2"), _tool("c2", _fail("no such file"))],
            session_id="sess-2",
        )
        d = build_digest(tmp_path, window_days=7)
        assert d["sessions_scanned"] == 2
        assert d["signals"]["tool_failures"] == {"terminal": 1, "read_file": 1}

    def test_window_excludes_old_dumps(self, tmp_path):
        _dump(
            tmp_path,
            "old",
            [_asst("terminal", "c1"), _tool("c1", _term(exit_code=127))],
            session_id="sess-old",
            age_days=30,
        )
        d = build_digest(tmp_path, window_days=7)
        assert d["sessions_scanned"] == 0

    def test_no_raw_text_from_error_or_messages(self, tmp_path):
        secret = "<REDACTED:email:db677acc382bd26bb3a00162f3e668d3> at 5 Main St"
        _dump(
            tmp_path,
            "d1",
            [
                _asst("terminal", "c1"),
                _tool("c1", _term("", exit_code=1, error=secret)),
            ],
            session_id="sess-1",
            error={
                "type": "bad_request",
                "status_code": 400,
                "message": secret,
                "response_text": secret,
                "body": secret,
            },
        )
        d = build_digest(tmp_path, window_days=7)
        # The failure is counted, but provider error contributes only status:type
        # and the digest never echoes the raw content.
        assert d["signals"]["tool_failures"] == {"terminal": 1}
        assert d["signals"]["provider_errors"] == {"400:bad_request": 1}
        assert secret not in json.dumps(d)

    def test_malformed_dump_does_not_crash(self, tmp_path):
        (tmp_path / "request_dump_bad.json").write_text("{ not json", encoding="utf-8")
        (tmp_path / "request_dump_list.json").write_text("[1,2,3]", encoding="utf-8")
        d = build_digest(tmp_path, window_days=7)
        assert d["sessions_scanned"] == 0  # both skipped, no exception

    def test_failure_category_preferred_over_raw_type(self, tmp_path):
        # #236: dumps now carry a structured failure_category; introspection keys
        # provider_errors by it (recovery class) so recurring bad provider-model
        # pairs group as e.g. 429:rate_limit instead of 429:RuntimeError (#237 pt3).
        _dump(
            tmp_path,
            "d1",
            [_asst("x", "c1"), _tool("c1", _term("ok"))],
            session_id="s1",
            error={
                "type": "RuntimeError",
                "status_code": 429,
                "failure_category": "rate_limit",
            },
        )
        d = build_digest(tmp_path, window_days=7)
        assert d["signals"]["provider_errors"] == {"429:rate_limit": 1}


# --- SessionDB state.db helpers (#399) ---------------------------------------


def _state_db(tmp_path, rows):
    """Create a minimal state.db messages table and insert ``rows``.

    Each row is a dict matching the SessionDB schema columns used by
    introspection_extract: session_id, role, content, tool_call_id,
    tool_calls, tool_name.  ``id`` is auto-incremented and drives order."""
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
            # Insert with explicit id when provided so tests can exercise
            # ordering independent of list order.
            params = (
                r["session_id"],
                r["role"],
                r.get("content"),
                r.get("tool_call_id"),
                json.dumps(r["tool_calls"]) if r.get("tool_calls") else None,
                r.get("tool_name"),
                time.time(),
            )
            if "id" in r:
                conn.execute(
                    "INSERT INTO messages (id, session_id, role, content, "
                    "tool_call_id, tool_calls, tool_name, timestamp) VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?)",
                    (r["id"],) + params,
                )
            else:
                conn.execute(
                    "INSERT INTO messages (session_id, role, content, tool_call_id, "
                    "tool_calls, tool_name, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    params,
                )
        conn.commit()
    finally:
        conn.close()
    return db_path


def _db_asst(tool, cid):
    """Assistant row for the state.db messages table."""
    return {
        "role": "assistant",
        "tool_calls": [{"id": cid, "function": {"name": tool, "arguments": "{}"}}],
    }


def _db_tool(cid, content):
    """Tool row for the state.db messages table."""
    return {"role": "tool", "tool_call_id": cid, "content": content}


class TestStateDB:
    """#399 — scripts/introspection_extract.py must scan the SQLite SessionDB
    (state.db messages table) in addition to JSONL and request_dump files."""

    def test_state_db_counts_sessions_and_signals(self, tmp_path):
        _state_db(
            tmp_path,
            [
                {"session_id": "sess-db-1", **_db_asst("terminal", "c1")},
                {
                    "session_id": "sess-db-1",
                    **_db_tool("c1", _term("bash: foo: not found", exit_code=127)),
                },
                {"session_id": "sess-db-2", **_db_asst("read_file", "c2")},
                {
                    "session_id": "sess-db-2",
                    **_db_tool("c2", _fail("no such file")),
                },
            ],
        )
        d = build_digest(tmp_path, window_days=7)
        assert d["sessions_scanned"] == 2
        assert d["signals"]["tool_failures"] == {"terminal": 1, "read_file": 1}

    def test_state_db_at_hermes_home_root(self, tmp_path, monkeypatch):
        """#623 — state.db can live at HERMES_HOME root, not under sessions_dir."""
        home = tmp_path / "home"
        home.mkdir()
        sessions_dir = home / "sessions"
        sessions_dir.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        _state_db(
            home,
            [
                {"session_id": "root-db", **_db_asst("terminal", "c1")},
                {
                    "session_id": "root-db",
                    **_db_tool("c1", _term("bash: foo: not found", exit_code=127)),
                },
            ],
        )
        d = build_digest(sessions_dir, window_days=7)
        assert d["sessions_scanned"] == 1
        assert d["signals"]["tool_failures"] == {"terminal": 1}

    def test_state_db_orders_by_id_for_tool_name_resolution(self, tmp_path):
        # Rows inserted with explicit ids in the wrong conversation order.
        # Ordering by id inside the session must reconstruct the correct order
        # so tool_call_id -> tool name resolution works.
        _state_db(
            tmp_path,
            [
                {"id": 1, "session_id": "s", **_db_asst("terminal", "c1")},
                {"id": 2, "session_id": "s", **_db_tool("c1", _fail("boom"))},
            ],
        )
        d = build_digest(tmp_path, window_days=7)
        assert d["signals"]["tool_failures"] == {"terminal": 1}

    def test_state_db_out_of_order_tool_result_is_unknown(self, tmp_path):
        # If a tool result row has a lower id than its matching assistant call,
        # we cannot attribute it (the assistant call hasn't been seen yet).
        # The scan must not crash and should count it as unknown.
        _state_db(
            tmp_path,
            [
                {"id": 2, "session_id": "s", **_db_asst("terminal", "c1")},
                {"id": 1, "session_id": "s", **_db_tool("c1", _fail("boom"))},
            ],
        )
        d = build_digest(tmp_path, window_days=7)
        assert d["signals"]["tool_failures"] == {"unknown": 1}

    def test_state_db_no_raw_text_in_digest(self, tmp_path):
        secret = "STATE_DB_SECRET <REDACTED:email:db677acc382bd26bb3a00162f3e668d3>"
        _state_db(
            tmp_path,
            [
                {"session_id": "s", **_db_asst("terminal", "c1")},
                {
                    "session_id": "s",
                    **_db_tool("c1", _term("", exit_code=1, error=secret)),
                },
            ],
        )
        d = build_digest(tmp_path, window_days=7)
        assert d["sessions_scanned"] == 1
        assert d["signals"]["tool_failures"] == {"terminal": 1}
        assert secret not in json.dumps(d)

    def test_state_db_skips_rows_without_role(self, tmp_path):
        _state_db(
            tmp_path,
            [
                {"session_id": "s", "role": "assistant", "content": "hello"},
                {"session_id": "s", "role": "", "content": "should be ignored"},
            ],
        )
        d = build_digest(tmp_path, window_days=7)
        assert d["sessions_scanned"] == 1
        assert d["signals"]["refusals_or_access_denied"] == 0

    def test_all_three_sources_aggregated(self, tmp_path):
        # JSONL session
        _session(
            tmp_path,
            "jsonl",
            [_asst("terminal", "c1"), _tool("c1", _term(exit_code=127))],
        )
        # request_dump session
        _dump(
            tmp_path,
            "dump",
            [_asst("read_file", "c2"), _tool("c2", _fail("no such file"))],
            session_id="sess-dump",
        )
        # state.db session
        _state_db(
            tmp_path,
            [
                {"session_id": "sess-db", **_db_asst("patch", "c3")},
                {"session_id": "sess-db", **_db_tool("c3", _ok(success=False))},
            ],
        )
        d = build_digest(tmp_path, window_days=7)
        assert d["sessions_scanned"] == 3
        assert d["signals"]["tool_failures"] == {
            "terminal": 1,
            "read_file": 1,
            "patch": 1,
        }


class TestRefusalRecovery:
    """A refusal that offers a way forward is not the failure #1327 is about.

    #1356 told the agent to propose an alternative before refusing for a missing
    capability. Counting both shapes as one number made that guidance
    unfalsifiable — the signal could not move even if it worked (#1366).
    """

    @staticmethod
    def _reply(text):
        return {"role": "assistant", "content": text}

    def test_bare_refusal_is_not_recovered(self, tmp_path):
        p = _session(tmp_path, "s", [self._reply("I can't do that — there is no tool for it.")])
        s = scan_session(p)
        assert s["refusals"] == 1
        assert s["refusals_with_recovery"] == 0

    def test_refusal_with_alternative_is_recovered(self, tmp_path):
        p = _session(
            tmp_path,
            "s",
            [self._reply("I can't call that API directly, but I can query it with a script instead.")],
        )
        s = scan_session(p)
        assert s["refusals"] == 1
        assert s["refusals_with_recovery"] == 1

    def test_however_pivot_counts(self, tmp_path):
        p = _session(
            tmp_path,
            "s",
            [self._reply("I cannot use grep. However, I can search with a regex pattern.")],
        )
        assert scan_session(p)["refusals_with_recovery"] == 1

    def test_workaround_phrasing_counts(self, tmp_path):
        p = _session(
            tmp_path, "s", [self._reply("No access to the database. A workaround is to export a dump.")]
        )
        assert scan_session(p)["refusals_with_recovery"] == 1

    def test_recovery_without_refusal_is_not_counted(self, tmp_path):
        """The marker only means something on a turn that actually refused."""
        p = _session(tmp_path, "s", [self._reply("Here is an alternative approach you might like.")])
        s = scan_session(p)
        assert s["refusals"] == 0
        assert s["refusals_with_recovery"] == 0

    def test_hedging_is_not_recovery(self, tmp_path):
        """Conservative by design — over-matching would make #1356 look effective
        when it is not, which is worse than under-counting."""
        p = _session(
            tmp_path, "s", [self._reply("I can't do that. Sorry about the inconvenience.")]
        )
        assert scan_session(p)["refusals_with_recovery"] == 0

    def test_digest_reports_rate(self, tmp_path):
        _session(tmp_path, "a", [self._reply("I can't do X, but I can do Y instead.")])
        _session(tmp_path, "b", [self._reply("I can't do Z.")])
        sig = build_digest(tmp_path, window_days=7)["signals"]
        assert sig["refusals_or_access_denied"] == 2
        assert sig["refusals_with_recovery"] == 1
        assert sig["refusal_recovery_rate"] == 0.5

    def test_rate_is_zero_with_no_refusals(self, tmp_path):
        _session(tmp_path, "a", [self._reply("Done — the file is written.")])
        sig = build_digest(tmp_path, window_days=7)["signals"]
        assert sig["refusals_or_access_denied"] == 0
        assert sig["refusal_recovery_rate"] == 0.0

    def test_existing_refusal_count_unchanged(self, tmp_path):
        """Back-compat: the recovered subset must not alter the aggregate."""
        _session(tmp_path, "a", [self._reply("I can't do X, but I can do Y instead.")])
        assert build_digest(tmp_path, window_days=7)["signals"]["refusals_or_access_denied"] == 1


class TestRefusalRecoveryFalsePositives:
    """Deflection and explanation are not recovery.

    Regression for a defect found in review: the pivot clause allowed second
    person, so "I can't access it, however you can try it yourself" counted as
    a recovery. That inverts the metric — it would improve whenever the agent
    got more polite about handing the work back, which is the exact behaviour
    #1327 wants to eliminate.
    """

    @staticmethod
    def _reply(text):
        return {"role": "assistant", "content": text}

    def _recovered(self, tmp_path, text, name="s"):
        p = _session(tmp_path, name, [self._reply(text)])
        s = scan_session(p)
        assert s["refusals"] == 1, "fixture must actually refuse"
        return s["refusals_with_recovery"]

    def test_deflection_to_user_is_not_recovery(self, tmp_path):
        assert self._recovered(
            tmp_path, "I can't access that URL, however you can try visiting it yourself."
        ) == 0

    def test_deflection_to_support_is_not_recovery(self, tmp_path):
        assert self._recovered(tmp_path, "I cannot do that. Instead, please contact support.") == 0

    def test_manual_handoff_is_not_recovery(self, tmp_path):
        assert self._recovered(
            tmp_path, "I don't have permission. But you will need to run it manually."
        ) == 0

    def test_offering_an_explanation_is_not_recovery(self, tmp_path):
        assert self._recovered(tmp_path, "I can't do that, but I can tell you why it failed.") == 0

    def test_first_person_alternative_still_counts(self, tmp_path):
        assert self._recovered(
            tmp_path, "I can't use grep directly, but I can search with a regex pattern."
        ) == 1

    def test_let_me_phrasing_counts(self, tmp_path):
        assert self._recovered(tmp_path, "I cannot do X. Let me try Y instead.") == 1

    def test_ill_write_a_script_counts(self, tmp_path):
        assert self._recovered(
            tmp_path, "I can't call the API directly. I'll write a script via terminal."
        ) == 1


class TestRefusalRecoveryNegationAndProximity:
    """The two failure modes that dominated the real corpus (#1366 round 2).

    Both were found by adversarial review of the first fix, which had narrowed
    the pivot to first person but left these untouched. Together they accounted
    for the rate falling from 0.2168 to 0.0839 on the same 143 refusals.
    """

    @staticmethod
    def _reply(text):
        return {"role": "assistant", "content": text}

    def _recovered(self, tmp_path, text, name="s"):
        p = _session(tmp_path, name, [self._reply(text)])
        s = scan_session(p)
        assert s["refusals"] == 1, "fixture must actually refuse"
        return s["refusals_with_recovery"]

    # --- negated modal inside the pivot itself ---------------------------
    # `\b` after "can" is satisfied by the apostrophe in "can't", so the pivot
    # clause matched inside the refusal it was supposed to follow.

    def test_but_i_cant_is_not_a_pivot(self, tmp_path):
        assert self._recovered(tmp_path, "But I can't just sit idle in a cron job.") == 0

    def test_though_i_cant_is_not_a_pivot(self, tmp_path):
        assert self._recovered(
            tmp_path, "Though I can't promise a fix today, someone should look tomorrow."
        ) == 0

    def test_however_we_cannot_is_not_a_pivot(self, tmp_path):
        assert self._recovered(tmp_path, "However, we cannot run the full analysis stage.") == 0

    # --- proximity -------------------------------------------------------
    # Long audit documents mention a third-party "cannot" and an unrelated
    # "instead" pages later; both used to land in the same count.

    def test_pivot_far_from_refusal_does_not_count(self, tmp_path):
        filler = "The report continues with unrelated detail. " * 12
        assert self._recovered(
            tmp_path, f"I cannot read that field. {filler} Instead, we let the cache expire."
        ) == 0

    def test_pivot_close_to_refusal_counts(self, tmp_path):
        assert self._recovered(
            tmp_path, "I cannot read that field directly. Instead, let me parse the raw envelope."
        ) == 1

    def test_pivot_before_refusal_does_not_count(self, tmp_path):
        """A plan followed by a refusal is not a refusal followed by a plan."""
        assert self._recovered(
            tmp_path, "Instead, let me check the cache. Then I hit the wall: I can't reach the API."
        ) == 0

    def test_second_refusal_in_the_message_can_still_recover(self, tmp_path):
        """Scanning every refusal, not just the first, keeps a real pivot late
        in a long turn visible."""
        assert self._recovered(
            tmp_path,
            "I cannot reach the primary host. Checking the mirror now — that also "
            "returned access denied, but I can fall back to the cached snapshot.",
        ) == 1
