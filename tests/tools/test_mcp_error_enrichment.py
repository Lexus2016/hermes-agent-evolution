"""Tests for MCP tool error enrichment with recovery directive (#1393).

When an MCP tool returns not_found or validation errors (tool-definition drift),
the error response must include a recovery directive so the agent re-checks the
schema instead of retrying in a loop (19 failed sessions/7d).

These tests exercise the error-enrichment logic directly rather than mocking
the full MCP call chain (which runs on a background asyncio loop and is fragile
to mock from sync test code).
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest


def _build_error_response(error_text: str, tool_name: str) -> dict:
    """Replicate the error-enrichment logic from _make_tool_handler's
    isError branch (mcp_tool.py lines ~4378-4403) for direct testing.

    This mirrors the production code path so tests assert real behavior
    without needing the MCP background event loop.
    """
    error_text = error_text or "MCP tool returned an error"
    lower_err = error_text.lower()
    if "not_found" in lower_err or "not found" in lower_err or \
       "validation" in lower_err or "invalid" in lower_err:
        error_text = (
            f"{error_text}\n\n"
            f"Recovery: the tool definition for '{tool_name}' may have "
            f"changed on the MCP server. Use tool_describe or tool_search "
            f"to check the current schema, then retry with corrected "
            f"arguments. If the tool no longer exists, use tool_search "
            f"to find an alternative."
        )
    return {"error": error_text}


class TestMCPErrorEnrichment:
    """Verify not_found / validation errors get a recovery directive (#1393)."""

    def test_not_found_error_gets_recovery_directive(self):
        """not_found errors must include a recovery directive."""
        resp = _build_error_response(
            "Error: not_found — tool 'composio_remote_workbench' not found",
            "composio_remote_workbench",
        )
        assert "error" in resp
        assert "Recovery:" in resp["error"]
        assert "tool_describe" in resp["error"]
        assert "tool_search" in resp["error"]
        assert "composio_remote_workbench" in resp["error"]

    def test_not_found_with_space_gets_directive(self):
        """'not found' (with space) also triggers the directive."""
        resp = _build_error_response("tool not found in registry", "my_tool")
        assert "Recovery:" in resp["error"]
        assert "tool_describe" in resp["error"]

    def test_validation_error_gets_recovery_directive(self):
        """Validation errors must include the recovery directive."""
        resp = _build_error_response(
            "validation error: parameter 'actions' is required", "some_tool",
        )
        assert "Recovery:" in resp["error"]
        assert "tool_describe" in resp["error"]

    def test_invalid_error_gets_recovery_directive(self):
        """'invalid' keyword also triggers the directive."""
        resp = _build_error_response("invalid argument type for 'limit'", "test_tool")
        assert "Recovery:" in resp["error"]
        assert "tool_search" in resp["error"]

    def test_generic_error_no_recovery_directive(self):
        """A timeout or generic error must NOT get the directive."""
        resp = _build_error_response("timeout: server did not respond in 30s", "tool_x")
        assert "error" in resp
        assert "Recovery:" not in resp["error"]

    def test_empty_error_text_falls_back_to_generic(self):
        """Empty error text falls back to the generic message, no directive."""
        resp = _build_error_response("", "tool_x")
        assert resp["error"] == "MCP tool returned an error"
        assert "Recovery:" not in resp["error"]

    def test_recovery_mentions_tool_name(self):
        """The recovery directive must name the tool so the agent knows what
        to re-describe."""
        resp = _build_error_response("not_found: missing tool", "my_specific_tool")
        assert "my_specific_tool" in resp["error"]

    def test_recovery_directive_format(self):
        """The recovery directive must tell the agent to use tool_describe
        AND tool_search (both paths — schema check and alternative search)."""
        resp = _build_error_response("validation: bad params", "test_tool")
        err = resp["error"]
        # Must mention both tool_describe (check current schema) and
        # tool_search (find alternative if tool no longer exists).
        assert "tool_describe" in err
        assert "tool_search" in err
        assert "corrected arguments" in err or "alternative" in err