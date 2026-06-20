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
