"""Tests for the 2026-08-03 evolution cycle fixes.

Covers:
- #1647: terminal parse-error enrichment + bare-string fallback
- #1648: memory tool 'other' error decomposition with recovery hint
"""

from __future__ import annotations

import json
import pytest
from unittest.mock import MagicMock, patch

from agent.tool_executor import _parse_tool_arguments


# ── #1647: parse-error enrichment ──────────────────────────────


class TestParseToolArgumentsEnrichment:
    """Verify _parse_tool_arguments returns enriched error messages."""

    def test_valid_json_dict_passes_through(self):
        """A valid JSON dict should parse with no error."""
        args, err = _parse_tool_arguments('{"command": "ls -la"}')
        assert err is None
        assert args == {"command": "ls -la"}

    def test_json_parse_error_is_enriched(self):
        """Malformed JSON should produce an error identifying the parse position."""
        args, err = _parse_tool_arguments('{"command": bad json}')
        assert err is not None
        result = json.loads(err)
        assert "JSON parse error" in result["error"]
        assert "position" in result["message"]

    def test_non_dict_json_is_identified(self):
        """A JSON list should produce a 'not a JSON object' error."""
        args, err = _parse_tool_arguments("[1, 2, 3]")
        assert err is not None
        result = json.loads(err)
        assert "not a JSON object" in result["error"]

    def test_none_arguments_produces_missing_error(self):
        """None arguments should produce a 'Missing' error."""
        args, err = _parse_tool_arguments(None)
        assert err is not None
        result = json.loads(err)
        assert "Missing" in result["error"]

    def test_non_string_arguments_produces_type_error(self):
        """A non-string argument should produce a type error."""
        args, err = _parse_tool_arguments(42)
        assert err is not None
        result = json.loads(err)
        assert "wrong type" in result["error"]


class TestTerminalBareStringFallback:
    """Verify the terminal bare-string fallback (issue #1647)."""

    def test_bare_string_terminal_is_extracted_as_command(self):
        """A bare command string for terminal should be extracted as {'command': ...}."""
        args, err = _parse_tool_arguments("ls -la /tmp", function_name="terminal")
        assert err is None
        assert args == {"command": "ls -la /tmp"}

    def test_bare_string_terminal_with_pipes(self):
        """Bare string with pipes should work too."""
        args, err = _parse_tool_arguments(
            "grep foo bar | wc -l", function_name="terminal"
        )
        assert err is None
        assert args == {"command": "grep foo bar | wc -l"}

    def test_json_object_terminal_still_works(self):
        """A valid JSON object for terminal should parse normally."""
        args, err = _parse_tool_arguments(
            '{"command": "echo hello"}', function_name="terminal"
        )
        assert err is None
        assert args == {"command": "echo hello"}

    def test_bare_string_non_terminal_still_errors(self):
        """Bare string for non-terminal tools should still error."""
        args, err = _parse_tool_arguments("some text", function_name="read_file")
        assert err is not None

    def test_empty_string_terminal_errors(self):
        """Empty string should not be extracted as a command."""
        args, err = _parse_tool_arguments("", function_name="terminal")
        assert err is not None

    def test_function_name_defaults_to_empty_string(self):
        """Calling without function_name should not crash."""
        args, err = _parse_tool_arguments('{"key": "value"}')
        assert err is None
        assert args == {"key": "value"}


# ── #1648: memory error decomposition ──────────────────────────


class TestMemoryErrorDecomposition:
    """Verify _memory_enriched_error classifies exception types."""

    def test_timeout_error_classified(self):
        from tools.memory_tool import _memory_enriched_error

        result = json.loads(_memory_enriched_error(TimeoutError("file lock"), "add"))
        assert result["error_category"] == "timeout"
        assert result["error_type"] == "TimeoutError"
        assert "locked" in result["recovery_hint"].lower()
        assert result["success"] is False

    def test_permission_error_classified(self):
        from tools.memory_tool import _memory_enriched_error

        result = json.loads(
            _memory_enriched_error(PermissionError("denied"), "replace")
        )
        assert result["error_category"] == "permission-denied"
        assert result["error_type"] == "PermissionError"

    def test_value_error_classified_as_schema(self):
        from tools.memory_tool import _memory_enriched_error

        result = json.loads(_memory_enriched_error(ValueError("bad data"), "add"))
        assert result["error_category"] == "schema-mismatch"

    def test_os_error_classified(self):
        from tools.memory_tool import _memory_enriched_error

        result = json.loads(_memory_enriched_error(OSError("disk full"), "remove"))
        assert result["error_category"] == "io-error"

    def test_type_error_classified(self):
        from tools.memory_tool import _memory_enriched_error

        result = json.loads(_memory_enriched_error(TypeError("bad type"), "add"))
        assert result["error_category"] == "serialization-error"

    def test_unexpected_error_classified(self):
        from tools.memory_tool import _memory_enriched_error

        result = json.loads(_memory_enriched_error(RuntimeError("weird"), "add"))
        assert result["error_category"] == "unexpected"
        assert "RuntimeError" in result["recovery_hint"]

    def test_all_results_have_required_fields(self):
        """Every enriched error should include error, error_type, category, hint, action."""
        from tools.memory_tool import _memory_enriched_error

        for exc in [
            TimeoutError("x"),
            PermissionError("x"),
            ValueError("x"),
            OSError("x"),
            RuntimeError("x"),
        ]:
            result = json.loads(_memory_enriched_error(exc, "add"))
            for field in (
                "error",
                "error_type",
                "error_category",
                "recovery_hint",
                "action",
                "success",
            ):
                assert field in result, (
                    f"Missing field {field} for {type(exc).__name__}"
                )
            assert result["success"] is False


class TestMemoryToolErrorIntegration:
    """Verify memory_tool dispatches enriched errors on store exceptions."""

    def test_add_failure_returns_enriched_error(self):
        from tools.memory_tool import memory_tool, MemoryStore

        store = MagicMock(spec=MemoryStore)
        store.add.side_effect = OSError("disk full")
        result = json.loads(memory_tool(action="add", content="test", store=store))
        assert result["error_category"] == "io-error"
        assert result["success"] is False

    def test_replace_failure_returns_enriched_error(self):
        from tools.memory_tool import memory_tool, MemoryStore

        store = MagicMock(spec=MemoryStore)
        store.replace.side_effect = PermissionError("denied")
        result = json.loads(
            memory_tool(action="replace", old_text="old", content="new", store=store)
        )
        assert result["error_category"] == "permission-denied"

    def test_remove_failure_returns_enriched_error(self):
        from tools.memory_tool import memory_tool, MemoryStore

        store = MagicMock(spec=MemoryStore)
        store.remove.side_effect = TimeoutError("lock")
        result = json.loads(memory_tool(action="remove", old_text="x", store=store))
        assert result["error_category"] == "timeout"
