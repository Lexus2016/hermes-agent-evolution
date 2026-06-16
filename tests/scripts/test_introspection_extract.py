"""Tests for scripts/introspection_extract.py — deterministic, anonymized digest (#89)."""

import json
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
    return {"role": "assistant", "tool_calls": [{"id": cid, "function": {"name": tool, "arguments": "{}"}}]}


def _tool(cid, content):
    return {"role": "tool", "tool_call_id": cid, "content": content}


class TestScanSession:
    def test_attributes_failures_to_tool(self, tmp_path):
        p = _session(tmp_path, "s1", [
            {"role": "session_meta"},
            _asst("terminal", "c1"), _tool("c1", "bash: foo: command not found"),
            _asst("terminal", "c2"), _tool("c2", "permission denied"),
            _asst("read_file", "c3"), _tool("c3", "ok, file contents here"),
        ])
        s = scan_session(p)
        assert s["tool_failures"] == {"terminal": 2}
        assert "read_file" not in s["tool_failures"]

    def test_counts_timeouts_and_refusals(self, tmp_path):
        p = _session(tmp_path, "s2", [
            _asst("mcp_health", "c1"), _tool("c1", "request timed out after 120s"),
            {"role": "assistant", "content": "I can't access that path."},
        ])
        s = scan_session(p)
        assert s["timeouts"] == 1
        assert s["refusals"] == 1

    def test_repeated_run_detected(self, tmp_path):
        lines = [{"role": "session_meta"}]
        for i in range(6):
            lines += [_asst("terminal", f"c{i}"), _tool(f"c{i}", "ok")]
        p = _session(tmp_path, "s3", lines)
        s = scan_session(p)
        assert s["repeated_tool_runs"].get("terminal") == 6

    def test_no_raw_text_in_output(self, tmp_path):
        secret = "USER SECRET email bob@example.com lives at 5 Main St"
        p = _session(tmp_path, "s4", [
            _asst("terminal", "c1"), _tool("c1", f"error: {secret}"),
        ])
        s = scan_session(p)
        # Digest carries only counts/tool names — never the raw content.
        assert secret not in json.dumps(s)


class TestBuildDigest:
    def test_window_excludes_old_sessions(self, tmp_path):
        _session(tmp_path, "recent", [_asst("terminal", "c1"), _tool("c1", "command not found")])
        _session(tmp_path, "old", [_asst("terminal", "c2"), _tool("c2", "command not found")], age_days=30)
        d = build_digest(tmp_path, window_days=7)
        assert d["sessions_scanned"] == 1
        assert d["signals"]["tool_failures"] == {"terminal": 1}

    def test_aggregates_across_sessions(self, tmp_path):
        for n in ("a", "b"):
            lines = [{"role": "session_meta"}]
            for i in range(5):
                lines += [_asst("terminal", f"{n}{i}"), _tool(f"{n}{i}", "ok")]
            _session(tmp_path, n, lines)
        d = build_digest(tmp_path, window_days=7)
        assert d["sessions_scanned"] == 2
        rr = d["signals"]["repeated_tool_runs"]["terminal"]
        assert rr["sessions"] == 2 and rr["max_consecutive"] == 5

    def test_missing_dir_is_empty(self, tmp_path):
        d = build_digest(tmp_path / "nope", window_days=7)
        assert d["sessions_scanned"] == 0


def _dump(tmp_path, name, messages, *, session_id, model="glm-5.2", error=None, age_days=0):
    obj = {
        "timestamp": "2026-06-16T00:00:00",
        "session_id": session_id,
        "reason": "error",
        "request": {"method": "POST", "url": "https://x/api", "headers": {},
                    "body": {"model": model, "messages": messages, "tools": []}},
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
        _dump(tmp_path, "d1", [
            _asst("terminal", "c1"), _tool("c1", "bash: foo: command not found"),
        ], session_id="sess-1", error={"type": "overloaded_error", "status_code": 529,
                                        "message": "x", "response_text": "y"})
        d = build_digest(tmp_path, window_days=7)
        assert d["sessions_scanned"] == 1
        assert d["signals"]["tool_failures"] == {"terminal": 1}
        assert d["signals"]["provider_errors"] == {"529:overloaded_error": 1}
        assert d["signals"]["models_used"] == {"glm-5.2": 1}

    def test_dedup_by_session_keeps_most_complete(self, tmp_path):
        # Two dumps of ONE session (growing prefix) count once, via the larger.
        short = [_asst("terminal", "c1"), _tool("c1", "permission denied")]
        full = short + [_asst("terminal", "c2"), _tool("c2", "command not found")]
        _dump(tmp_path, "early", short, session_id="sess-1")
        _dump(tmp_path, "late", full, session_id="sess-1")
        d = build_digest(tmp_path, window_days=7)
        assert d["sessions_scanned"] == 1  # one session, not two dumps
        assert d["signals"]["tool_failures"] == {"terminal": 2}  # from the full one

    def test_mixed_jsonl_and_dump_both_counted(self, tmp_path):
        _session(tmp_path, "s1", [_asst("terminal", "c1"), _tool("c1", "command not found")])
        _dump(tmp_path, "d1", [_asst("read_file", "c2"), _tool("c2", "no such file")],
              session_id="sess-2")
        d = build_digest(tmp_path, window_days=7)
        assert d["sessions_scanned"] == 2
        assert d["signals"]["tool_failures"] == {"terminal": 1, "read_file": 1}

    def test_window_excludes_old_dumps(self, tmp_path):
        _dump(tmp_path, "old", [_asst("terminal", "c1"), _tool("c1", "command not found")],
              session_id="sess-old", age_days=30)
        d = build_digest(tmp_path, window_days=7)
        assert d["sessions_scanned"] == 0

    def test_no_raw_text_from_error_or_messages(self, tmp_path):
        secret = "bob@example.com at 5 Main St"
        _dump(tmp_path, "d1", [
            _asst("terminal", "c1"), _tool("c1", f"error: {secret}"),
        ], session_id="sess-1", error={"type": "bad_request", "status_code": 400,
                                       "message": secret, "response_text": secret,
                                       "body": secret})
        d = build_digest(tmp_path, window_days=7)
        # Provider error contributes only status:type — never the echoed content.
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
        _dump(tmp_path, "d1", [_asst("x", "c1"), _tool("c1", "ok")],
              session_id="s1", error={"type": "RuntimeError", "status_code": 429,
                                      "failure_category": "rate_limit"})
        d = build_digest(tmp_path, window_days=7)
        assert d["signals"]["provider_errors"] == {"429:rate_limit": 1}
