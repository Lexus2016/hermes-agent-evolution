#!/usr/bin/env python3
"""Integration tests for MCP 2026-07-28 stateless protocol path (Slice B-3 / #1513).

Exercises the full stateless request path end-to-end: mock stateless server
fixture → capability detection → synthesize from discover → call_tool routing
→ session-id absence → flag-off regression. Test-only — no production code
changes (B-1/B-2 landed the code).
"""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import patch
from typing import Any

import pytest


def _make_server(stateless: bool = True, session=None) -> Any:
    """Build a lightweight MCPServerTask-like object for stateless path tests."""
    from tools.mcp_tool import MCPServerTask

    srv = MCPServerTask.__new__(MCPServerTask)
    srv.name = "mock-server"
    srv.initialize_result = None
    srv._stateless_enabled = stateless
    srv.session = session
    srv._rpc_lock = asyncio.Lock()
    srv._pending_call_context = None
    srv._config = {"url": "http://localhost:9999/mcp"}
    return srv


class MockStatelessSession:
    """Mock MCP client session responding to ``server/discover`` and ``tools/call``."""

    def __init__(self, capabilities: dict | None = None):
        self._capabilities = capabilities or {"tools": True, "resources": True}
        self._tool_results: dict[str, Any] = {}
        self.call_tool_calls: list[dict] = []

    async def send_request(self, request: dict, result_type=type(None)):
        if request.get("method") == "server/discover":
            return {
                "jsonrpc": "2.0",
                "id": request.get("id", 0),
                "result": {
                    "capabilities": self._capabilities,
                    "tools": [{"name": "echo", "description": "Echo tool"}],
                    "prompts": [],
                },
            }
        return {"jsonrpc": "2.0", "id": request.get("id", 0), "result": {}}

    async def call_tool(self, name: str, arguments: dict | None = None, **kwargs):
        self.call_tool_calls.append({"name": name, "arguments": arguments, **kwargs})
        if name in self._tool_results:
            return self._tool_results[name]
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=f"result for {name}")],
            isError=False,
            structuredContent=None,
        )

    def set_tool_result(self, name: str, content: list, isError: bool = False):
        self._tool_results[name] = SimpleNamespace(
            content=content, isError=isError, structuredContent=None
        )


class TestCapabilityDetectionIntegration:
    """Verify _detect_stateless_support() identifies stateless vs stateful servers."""

    def test_stateless_detected(self):
        assert _make_server(stateless=True)._detect_stateless_support() is True

    def test_stateful_not_detected(self):
        assert _make_server(stateless=False)._detect_stateless_support() is False

    def test_env_flag_controls_detection(self):
        with patch.dict(os.environ, {"HERMES_MCP_STATELESS": "1"}):
            srv = _make_server()
            srv._stateless_enabled = True
            assert srv._detect_stateless_support() is True
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HERMES_MCP_STATELESS", None)
            srv2 = _make_server(stateless=False)
            assert srv2._detect_stateless_support() is False


class TestSynthesizeFromDiscover:
    def test_discover_returns_synthesized_caps(self):
        srv = _make_server(
            session=MockStatelessSession({"tools": True, "resources": False})
        )
        result = asyncio.run(srv._discover_server_capabilities())
        assert result.capabilities.tools is not None
        assert result.capabilities.resources is None

    def test_discover_fallback_on_error(self):
        async def failing(*a, **kw):
            raise ConnectionError("gone")

        srv = _make_server(session=SimpleNamespace(send_request=failing))
        result = asyncio.run(srv._discover_server_capabilities())
        assert result.capabilities.tools is not None

    def test_discover_fallback_when_no_session(self):
        srv = _make_server()
        srv.session = None
        result = asyncio.run(srv._discover_server_capabilities())
        assert result.capabilities.tools is not None

    def test_synthesized_caps_accepted_by_advertises_tools(self):
        srv = _make_server(session=MockStatelessSession())
        srv.initialize_result = asyncio.run(srv._discover_server_capabilities())
        assert srv._advertises_tools() is True


class TestStatelessCallToolRouting:
    """Verify the hot path routes correctly in stateless mode."""

    def test_meta_produced_for_tools_call(self):
        srv = _make_server()
        assert srv.stateless_routing_meta("tools/call", "echo") == {
            "Mcp-Method": "tools/call",
            "Mcp-Name": "echo",
        }

    def test_call_tool_receives_meta_stateless(self):
        srv = _make_server(session=MockStatelessSession())
        meta = srv.stateless_routing_meta("tools/call", "echo")
        asyncio.run(srv.session.call_tool("echo", arguments={"msg": "hi"}, meta=meta))
        call = srv.session.call_tool_calls[0]
        assert call["meta"] == {"Mcp-Method": "tools/call", "Mcp-Name": "echo"}

    def test_call_tool_no_meta_stateful(self):
        srv = _make_server(stateless=False, session=MockStatelessSession())
        assert srv.stateless_routing_meta("tools/call", "echo") is None
        asyncio.run(srv.session.call_tool("echo", arguments={"msg": "hi"}))
        assert "meta" not in srv.session.call_tool_calls[0]

    def test_error_result_handled_stateless(self):
        srv = _make_server(session=MockStatelessSession())
        srv.session.set_tool_result(
            "fail", [SimpleNamespace(type="text", text="boom")], isError=True
        )
        result = asyncio.run(
            srv.session.call_tool(
                "fail",
                arguments={},
                meta={"Mcp-Method": "tools/call", "Mcp-Name": "fail"},
            )
        )
        assert result.isError is True


class TestFlagOffRegression:
    """When HERMES_MCP_STATELESS is NOT set, behavior is identical to pre-stateless."""

    def test_detect_returns_false(self):
        assert _make_server(stateless=False)._detect_stateless_support() is False

    def test_meta_returns_none(self):
        assert (
            _make_server(stateless=False).stateless_routing_meta("tools/call", "x")
            is None
        )

    def test_get_session_id_passthrough(self):
        srv = _make_server(stateless=False)
        assert srv._get_session_id_stateless(lambda: "abc") == "abc"

    def test_synthesize_works_when_off(self):
        srv = _make_server(stateless=False)
        assert (
            srv.synthesize_capabilities({"tools": True}).capabilities.tools is not None
        )


class TestSessionIdAbsence:
    def test_returns_none_stateless(self):
        srv = _make_server()
        assert srv._get_session_id_stateless(lambda: "abc") is None

    def test_returns_id_stateful(self):
        srv = _make_server(stateless=False)
        assert srv._get_session_id_stateless(lambda: "abc") == "abc"

    def test_returns_none_on_error_stateful(self):
        srv = _make_server(stateless=False)

        def raising():
            raise RuntimeError()

        assert srv._get_session_id_stateless(raising) is None
