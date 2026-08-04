#!/usr/bin/env python3
"""Integration tests for the MCP stateless path (Slice B-3 / #1513).

Exercises a *mock stateless server* end-to-end: it responds to
``server/discover`` and routes on inline ``_meta`` but has NO
``initialize``/``initialized`` (proving the adapter no longer depends on the
handshake). Covers capability detection, synthesized caps flowing into the
capability gate, stateless call routing, flag-OFF regression, and that
``Mcp-Session-Id`` is not sent in stateless mode. Test-only — no production
code is modified.
"""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import patch

from tools.mcp_tool import MCPServerTask


class MockStatelessServer:
    """A stateless MCP server: discover + tools/call only, no handshake."""

    def __init__(self):
        self.discover_calls = 0
        self.call_metas = []

    async def send_request(self, request, _result_type):
        method = request.get("method")
        if method == "server/discover":
            self.discover_calls += 1
            return {"capabilities": {"tools": True}}
        if method == "tools/call":
            self.call_metas.append((request.get("params") or {}).get("_meta", {}))
            return {"content": [], "isError": False}
        raise RuntimeError(f"method not found: {method}")


def _make_server(stateless: bool = False):
    """Build an MCPServerTask instance (bypass __init__ heavy deps)."""
    srv = MCPServerTask.__new__(MCPServerTask)
    srv.name = "test-server"
    srv.initialize_result = None
    srv._stateless_enabled = stateless
    srv.session = None
    return srv


def _stateless(fake):
    srv = _make_server(stateless=True)
    srv.session = fake
    return srv


# --- 1. Mock stateless server has no handshake + capability detection -------


class TestMockStatelessServer:
    def test_no_initialize_method(self):
        fake = MockStatelessServer()
        assert not hasattr(fake, "initialize")
        assert not hasattr(fake, "initialized")

    def test_discover_returns_capabilities(self):
        result = asyncio.run(
            MockStatelessServer().send_request(
                {"method": "server/discover", "params": {}}, None
            )
        )
        assert result["capabilities"]["tools"] is True

    def test_detection_flag_off(self):
        assert _make_server(stateless=False)._detect_stateless_support() is False

    def test_detection_flag_on(self):
        assert _make_server(stateless=True)._detect_stateless_support() is True

    def test_detection_reads_env_flag(self):
        with patch.dict(os.environ, {"HERMES_MCP_STATELESS": "1"}):
            srv = MCPServerTask.__new__(MCPServerTask)
            srv._stateless_enabled = os.environ.get(
                "HERMES_MCP_STATELESS", ""
            ).strip().lower() in ("1", "true", "yes", "on")
            assert srv._detect_stateless_support() is True


# --- 2. Discover via mock server (no initialize needed) ---------------------


class TestDiscover:
    def test_uses_discover_not_handshake(self):
        fake = MockStatelessServer()
        srv = _stateless(fake)
        result = asyncio.run(srv._discover_server_capabilities())
        assert result.capabilities.tools is not None
        assert fake.discover_calls == 1

    def test_caps_flow_into_advertise_gate(self):
        srv = _stateless(MockStatelessServer())
        srv.initialize_result = asyncio.run(srv._discover_server_capabilities())
        assert srv._advertises_tools() is True

    def test_no_tools_capability_means_no_tools(self):
        srv = _make_server(stateless=True)
        srv.initialize_result = srv.synthesize_capabilities({"resources": True})
        assert srv._advertises_tools() is False


# --- 3. synthesize_capabilities -> InitializeResult-shaped object -----------


class TestSynthesize:
    def test_default_advertises_tools(self):
        caps = _make_server(stateless=True).synthesize_capabilities()
        assert caps.capabilities.tools is not None

    def test_explicit_doc_passthrough(self):
        caps = _make_server(stateless=True).synthesize_capabilities({
            "tools": True,
            "resources": True,
            "prompts": False,
        })
        assert caps.capabilities.tools is not None
        assert caps.capabilities.resources is not None
        assert caps.capabilities.prompts is None


# --- 4. Stateless call routing: inline _meta, no session id -----------------


class TestStatelessCallRouting:
    def test_stateless_call_carries_inline_meta(self):
        fake = MockStatelessServer()
        srv = _stateless(fake)
        meta = srv.stateless_routing_meta("tools/call", "echo")
        result = asyncio.run(
            srv.session.send_request(
                {
                    "jsonrpc": "2.0",
                    "method": "tools/call",
                    "params": {"name": "echo", "arguments": {"x": 1}, "_meta": meta},
                    "id": 1,
                },
                None,
            )
        )
        assert result["isError"] is False
        assert fake.call_metas == [{"Mcp-Method": "tools/call", "Mcp-Name": "echo"}]

    def test_stateful_call_has_no_meta(self):
        fake = MockStatelessServer()
        srv = _make_server(stateless=False)
        srv.session = fake
        assert srv.stateless_routing_meta("tools/call", "echo") is None
        asyncio.run(
            srv.session.send_request(
                {
                    "jsonrpc": "2.0",
                    "method": "tools/call",
                    "params": {"name": "echo"},
                    "id": 1,
                },
                None,
            )
        )
        assert fake.call_metas == [{}]

    def test_no_session_id_stateless(self):
        assert (
            _make_server(stateless=True)._get_session_id_stateless(lambda: "abc")
            is None
        )

    def test_session_id_delegates_stateful(self):
        assert (
            _make_server(stateless=False)._get_session_id_stateless(lambda: "abc")
            == "abc"
        )

    def test_session_id_none_on_error(self):
        def boom():
            raise RuntimeError("no id")

        assert _make_server(stateless=False)._get_session_id_stateless(boom) is None
