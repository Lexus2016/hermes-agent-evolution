"""Tests for the enriched non-deferrable tool_call error path (#1392).

When agents (especially subagents without terminal per #1307) try to invoke
a core tool via tool_call, the error must guide recovery instead of a generic
"not a deferrable tool" that triggers retry loops (57 errors/7d).
"""
from __future__ import annotations

from unittest.mock import patch

from tools.tool_search import (
    ToolSearchConfig,
    _non_deferrable_error,
    _CORE_TOOL_ALTERNATIVES,
    resolve_underlying_call,
)


class TestNonDeferrableError:
    """Verify the error message is actionable for the agent (#1392)."""

    def _mock_core(self, tools: frozenset):
        return patch("tools.tool_search.effective_core_tool_names",
                      return_value=tools)

    def test_terminal_unavailable_shows_alternatives(self):
        with self._mock_core(frozenset({"read_file", "write_file", "patch"})):
            msg = _non_deferrable_error("terminal")
        assert "terminal" in msg
        assert "not available in this environment" in msg
        for alt in ("search_files", "read_file", "patch", "delegate_task"):
            assert alt in msg, f"Missing alternative {alt}: {msg}"

    def test_terminal_available_says_call_directly(self):
        with self._mock_core(frozenset({"terminal", "read_file"})):
            msg = _non_deferrable_error("terminal")
        assert "core tool" in msg
        assert "Call it directly" in msg
        assert "tool_call" in msg

    def test_execute_code_unavailable_shows_delegate(self):
        with self._mock_core(frozenset({"read_file"})):
            msg = _non_deferrable_error("execute_code")
        assert "execute_code" in msg and "not available" in msg
        assert "delegate_task" in msg

    def test_browser_navigate_unavailable_shows_web_tools(self):
        with self._mock_core(frozenset({"read_file"})):
            msg = _non_deferrable_error("browser_navigate")
        assert "browser_navigate" in msg and "not available" in msg
        assert "web_search" in msg and "web_extract" in msg

    def test_unknown_tool_generic_message(self):
        msg = _non_deferrable_error("xx_definitely_not_a_tool_xx")
        assert "not a deferrable tool" in msg
        assert "tool_search" in msg or "call it directly" in msg

    def test_case_insensitive_lookup_preserves_name(self):
        with self._mock_core(frozenset({"Terminal"})):
            msg = _non_deferrable_error("Terminal")
        assert "core tool" in msg and "Call it directly" in msg

    def test_all_alternatives_have_recovery_guidance(self):
        """Every entry in _CORE_TOOL_ALTERNATIVES must mention at least one
        alternative tool name so the agent can change strategy."""
        for tool_name in _CORE_TOOL_ALTERNATIVES:
            with self._mock_core(frozenset()):
                msg = _non_deferrable_error(tool_name)
            assert tool_name in msg and "not available" in msg
            assert any(a in msg for a in (
                "search_files", "read_file", "patch", "write_file",
                "delegate_task", "web_search", "web_extract", "terminal",
            )), f"No recovery tools in message for {tool_name}: {msg}"


class TestResolveUnderlyingCallError:
    """Integration: resolve_underlying_call returns the enriched error."""

    def test_terminal_returns_enriched_error(self):
        cfg = ToolSearchConfig.from_raw({"enabled": "on"})
        _name, _args, err = resolve_underlying_call(
            {"name": "terminal", "arguments": {}}, cfg)
        assert err is not None
        # Must NOT be the old generic-only message.
        assert "tool_search" in err or "not available" in err or \
               "Call it directly" in err, f"Not enriched: {err}"

    def test_unknown_tool_returns_error(self):
        cfg = ToolSearchConfig.from_raw({"enabled": "on"})
        _name, _args, err = resolve_underlying_call(
            {"name": "xx_not_a_tool", "arguments": {}}, cfg)
        assert err is not None and "not a deferrable" in err

    def test_available_core_tool_mentions_direct_call(self):
        cfg = ToolSearchConfig.from_raw({"enabled": "on"})
        _name, _args, err = resolve_underlying_call(
            {"name": "read_file", "arguments": {}}, cfg)
        assert err is not None
        assert "call it directly" in err.lower() or "Call it directly" in err or \
               "tool_search" in err, f"Not actionable: {err}"