"""Tests for agent/tool_diagnostics.py — normalized failure taxonomy (#130/#175)."""

from agent.tool_diagnostics import classify, diagnostic_suffix
from agent.tool_dispatch_helpers import make_tool_result_message


class TestClassify:
    def test_success_is_none(self):
        assert classify("ok, wrote 3 files") is None
        assert classify("") is None
        assert classify(None) is None

    def test_missing_command(self):
        cat, hint = classify("bash: foo: command not found")
        assert cat == "missing_command" and "prerequisites" in hint.lower()

    def test_permission(self):
        assert classify("Refusing to write to sensitive system path")[0] == "permission"
        assert classify("error: permission denied")[0] in ("permission", "missing_command", "runtime_error")

    def test_timeout(self):
        assert classify("request timed out after 120s")[0] == "timeout"
        assert classify("ClosedResourceError: server unreachable")[0] == "timeout"

    def test_limit(self):
        assert classify("value exceeds the maximum length of 2200 characters")[0] == "limit"

    def test_not_found(self):
        assert classify("grep: no matches found")[0] == "not_found"

    def test_runtime_error_fallback(self):
        assert classify("Traceback (most recent call last):\n  ...")[0] == "runtime_error"
        assert classify("process exited, exit code: 1")[0] == "runtime_error"


class TestDiagnosticSuffix:
    def test_empty_for_success(self):
        assert diagnostic_suffix("done, all good") == ""

    def test_suffix_for_failure(self):
        s = diagnostic_suffix("permission denied")
        assert s.startswith("\n\n[diagnostic] failure-class=") and "permission" in s


class TestWiredIntoToolResult:
    def test_failure_result_gets_hint(self):
        msg = make_tool_result_message("terminal", "bash: x: command not found", "c1")
        assert "[diagnostic] failure-class=missing_command" in msg["content"]

    def test_success_result_unchanged(self):
        msg = make_tool_result_message("read_file", "file contents, all fine", "c2")
        assert "[diagnostic]" not in msg["content"]
