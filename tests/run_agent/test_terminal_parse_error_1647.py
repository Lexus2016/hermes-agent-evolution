"""Tests for issue #1647 (terminal parse-error bare-string fallback).

Terminal tool calls frequently arrive as bare strings from the model instead
of the schema's JSON object shape. This asserts the fallback: a quoted or
unquoted command string maps onto the ``command`` field and executes instead
of being rejected as a parse error, while non-terminal tools keep the strict
object-only contract (with a tool-name + parse-detail enriched error).
"""

import json

from agent.tool_executor import _parse_tool_arguments


class TestTerminalBareStringFallback:
    def test_valid_json_object_still_parses(self):
        """A normal JSON object argument dict passes through unchanged."""
        args, err = _parse_tool_arguments(
            '{"command": "ls -la", "timeout": 10}', tool_name="terminal"
        )
        assert err is None
        assert args == {"command": "ls -la", "timeout": 10}

    def test_quoted_bare_string_becomes_command(self):
        """A quoted JSON string ('"ls -la"') maps to the command field."""
        args, err = _parse_tool_arguments('"ls -la"', tool_name="terminal")
        assert err is None
        assert args == {"command": "ls -la"}

    def test_unquoted_bare_string_becomes_command(self):
        """An unquoted command that fails json.loads maps to the command field."""
        args, err = _parse_tool_arguments("ls -la /tmp", tool_name="terminal")
        assert err is None
        assert args == {"command": "ls -la /tmp"}

    def test_empty_bare_string_rejected(self):
        """An empty/whitespace bare string still produces a parse error."""
        args, err = _parse_tool_arguments("   ", tool_name="terminal")
        assert args == {}
        assert err is not None
        assert "tool was not executed" in err

    def test_non_terminal_tool_unchanged(self):
        """Bare-string fallback only applies to the terminal tool."""
        args, err = _parse_tool_arguments("not json", tool_name="web_search")
        assert args == {}
        assert err is not None
        assert "web_search" in err  # tool name surfaced for self-correction

    def test_error_includes_parse_detail_for_self_correction(self):
        """The error message names the tool and the underlying parse failure."""
        args, err = _parse_tool_arguments("[[[", tool_name="web_search")
        assert args == {}
        assert err is not None
        payload = json.loads(err)
        assert "web_search" in payload["message"]
        assert "JSONDecodeError" in payload["message"]
