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
