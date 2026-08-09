"""Tests for evolution spiral-cap and error-enrichment fixes (#1839, #1840, #1841, #1842)."""

import json

from agent.display import _detect_tool_failure
from agent.loop_guard import _is_non_retryable, _NON_RETRYABLE_BY_TOOL
from agent.tool_diagnostics import classify
from agent.tool_guardrails import (
    classify_tool_failure,
    _SPIRAL_PRONE_TOOLS,
    _TOOL_FALLBACK_DIRECTIVE,
)
from agent.loop_guard import _NON_RETRYABLE


class TestProcessFailureDetection:
    """#1839: process non-zero exit_code must be counted as failure."""

    def test_classify_tool_failure_process_nonzero_exit(self):
        result = json.dumps({"session_id": "abc", "status": "exited", "exit_code": 1})
        failed, suffix = classify_tool_failure("process", result)
        assert failed is True
        assert "exit 1" in suffix

    def test_classify_tool_failure_process_zero_exit(self):
        result = json.dumps({"session_id": "abc", "status": "exited", "exit_code": 0})
        assert classify_tool_failure("process", result)[0] is False

    def test_classify_tool_failure_process_running(self):
        result = json.dumps({"session_id": "abc", "status": "running"})
        assert classify_tool_failure("process", result)[0] is False

    def test_detect_tool_failure_process_nonzero_exit(self):
        result = json.dumps({"session_id": "abc", "status": "exited", "exit_code": -1})
        failed, suffix = _detect_tool_failure("process", result)
        assert failed is True
        assert "exit" in suffix.lower()

    def test_detect_tool_failure_process_error_with_exit(self):
        result = json.dumps({"status": "not_found", "exit_code": 1, "error": "No process with ID xyz"})
        failed, suffix = _detect_tool_failure("process", result)
        assert failed is True
        assert "No process" in suffix


class TestWriteFileSpiralCap:
    """#1840: write_file in spiral-prone tools + parse_error non-retryable."""

    def test_write_file_in_spiral_prone_tools(self):
        assert "write_file" in _SPIRAL_PRONE_TOOLS

    def test_parse_error_non_retryable_for_write_file(self):
        assert _is_non_retryable("write_file", "parse_error") is True

    def test_parse_error_non_retryable_for_terminal(self):
        assert _is_non_retryable("terminal", "parse_error") is True

    def test_write_file_in_non_retryable_by_tool(self):
        assert "parse_error" in _NON_RETRYABLE_BY_TOOL.get("write_file", frozenset())


class TestTerminalTimeoutShouldRetry:
    """#1841: terminal timeout should_retry=False."""

    def test_timeout_error_should_retry_false(self):
        result = json.dumps({"output": "", "exit_code": 124, "error": "Command timed out after 180 seconds. This command looks long-running — re-run with background=true and notify_on_complete=true.", "should_retry": False})
        data = json.loads(result)
        assert data["should_retry"] is False
        assert "timed out" in data["error"].lower()
        assert "background=true" in data["error"]

    def test_timeout_non_retryable_globally(self):
        assert "timeout" in _NON_RETRYABLE

    def test_terminal_fallback_directive_mentions_background(self):
        assert "background=true" in _TOOL_FALLBACK_DIRECTIVE.get("terminal", "")


class TestPatchAmbiguousDiagnostics:
    """#1842: patch ambiguous category + non-retryable + parse_error."""

    def test_classify_ambiguous_error(self):
        msg = "Found 3 matches for old_string at:\n  Line 10: def foo()\n  Line 25: def foo()"
        result = classify(msg)
        assert result is not None
        assert result[0] == "ambiguous"

    def test_ambiguous_non_retryable_for_patch(self):
        assert _is_non_retryable("patch", "ambiguous") is True

    def test_ambiguous_not_non_retryable_for_terminal(self):
        assert _is_non_retryable("terminal", "ambiguous") is False

    def test_patch_non_retryable_by_tool(self):
        tool_classes = _NON_RETRYABLE_BY_TOOL.get("patch", frozenset())
        assert "ambiguous" in tool_classes
        assert "not_found" in tool_classes

    def test_classify_parse_error_json(self):
        msg = "Refusing to write 'config.json': candidate content fails .json syntax validation (JSONDecodeError: Expecting value: line 1, column 1)."
        assert classify(msg)[0] == "parse_error"

    def test_classify_parse_error_yaml(self):
        msg = "Refusing to write 'config.yaml': candidate content fails .yaml syntax validation (YAMLError: while parsing a block mapping)."
        assert classify(msg)[0] == "parse_error"