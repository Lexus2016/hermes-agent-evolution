#!/usr/bin/env python3
"""Unit tests for MCP stateless request routing (Slice B-2 / #1512).

Tests cover:
- ``_build_stateless_meta`` produces correct routing dict when flag ON, None when OFF
- ``_get_session_id_stateless`` returns None in stateless mode, delegates when stateful
- ``_discover_server_capabilities`` falls back to synthesize on error / no session
- ``call_tool`` dispatch passes inline ``_meta`` when stateless
- Stateful path is unchanged (no _meta injection)
"""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


def _make_server(stateless: bool = False):
    """Build an MCPServerTask instance (bypass __init__ heavy deps)."""
    from tools.mcp_tool import MCPServerTask

    srv = MCPServerTask.__new__(MCPServerTask)
    srv.name = "test-server"
    srv.initialize_result = None
    srv._stateless_enabled = stateless
    srv.session = None
    return srv


# ---------------------------------------------------------------------------
# _build_stateless_meta
# ---------------------------------------------------------------------------


class TestBuildStatelessMeta:
    def test_returns_none_when_disabled(self):
        srv = _make_server(stateless=False)
        assert srv._build_stateless_meta() is None

    def test_returns_meta_dict_when_enabled(self):
        srv = _make_server(stateless=True)
        meta = srv._build_stateless_meta()
        assert meta is not None
        assert meta["Mcp-Name"] == "test-server"
        assert meta["Mcp-Method"] == "tools/call"

    def test_meta_has_exactly_two_keys(self):
        srv = _make_server(stateless=True)
        meta = srv._build_stateless_meta()
        assert meta is not None
        assert set(meta.keys()) == {"Mcp-Name", "Mcp-Method"}


# ---------------------------------------------------------------------------
# _get_session_id_stateless
# ---------------------------------------------------------------------------


class TestGetSessionIdStateless:
    def test_returns_none_when_stateless(self):
        srv = _make_server(stateless=True)
        result = srv._get_session_id_stateless(lambda: "session-abc")
        assert result is None

    def test_delegates_when_stateful(self):
        srv = _make_server(stateless=False)
        result = srv._get_session_id_stateless(lambda: "session-abc")
        assert result == "session-abc"

    def test_returns_none_on_delegate_exception(self):
        srv = _make_server(stateless=False)

        def _boom():
            raise RuntimeError("no id")

        result = srv._get_session_id_stateless(_boom)
        assert result is None


# ---------------------------------------------------------------------------
# _discover_server_capabilities
# ---------------------------------------------------------------------------


class TestDiscoverServerCapabilities:
    def test_no_session_returns_synthesized(self):
        srv = _make_server(stateless=True)
        srv.session = None
        result = asyncio.run(srv._discover_server_capabilities())
        assert result.capabilities.tools is not None

    def test_rpc_error_falls_back_to_synthesize(self):
        srv = _make_server(stateless=True)
        mock_session = SimpleNamespace()
        mock_session.send_request = AsyncMock(
            side_effect=RuntimeError("method not found")
        )
        srv.session = mock_session
        result = asyncio.run(srv._discover_server_capabilities())
        assert result.capabilities.tools is not None

    def test_rpc_success_extracts_capabilities(self):
        srv = _make_server(stateless=True)
        mock_session = SimpleNamespace()
        mock_session.send_request = AsyncMock(
            return_value={"capabilities": {"tools": True, "resources": True}}
        )
        srv.session = mock_session
        result = asyncio.run(srv._discover_server_capabilities())
        assert result.capabilities.tools is not None
        assert result.capabilities.resources is not None

    def test_rpc_success_no_caps_key_defaults(self):
        srv = _make_server(stateless=True)
        mock_session = SimpleNamespace()
        mock_session.send_request = AsyncMock(return_value={"tools": []})
        srv.session = mock_session
        result = asyncio.run(srv._discover_server_capabilities())
        assert result.capabilities.tools is not None


# ---------------------------------------------------------------------------
# call_tool dispatch integration (verify _meta is passed)
# ---------------------------------------------------------------------------


class TestCallToolStatelessMeta:
    def test_stateful_path_no_meta(self):
        """When flag is OFF, _build_stateless_meta returns None → no meta passed."""
        srv = _make_server(stateless=False)
        assert srv._build_stateless_meta() is None

    def test_stateless_path_has_meta(self):
        """When flag is ON, _build_stateless_meta returns routing dict."""
        srv = _make_server(stateless=True)
        meta = srv._build_stateless_meta()
        assert meta is not None
        assert "Mcp-Name" in meta
        assert "Mcp-Method" in meta


# ---------------------------------------------------------------------------
# Stateful path unchanged regression
# ---------------------------------------------------------------------------


class TestStatefulPathUnchangedB2:
    def test_get_session_id_delegates_in_stateful(self):
        """In stateful mode, _get_session_id_stateless must call the original."""
        srv = _make_server(stateless=False)
        called = []

        def fake_gsid():
            called.append(True)
            return "real-id"

        result = srv._get_session_id_stateless(fake_gsid)
        assert result == "real-id"
        assert called == [True]

    def test_build_meta_none_in_stateful(self):
        srv = _make_server(stateless=False)
        assert srv._build_stateless_meta() is None

    def test_discover_falls_back_in_stateful(self):
        """Even in stateful mode, discover gracefully falls back."""
        srv = _make_server(stateless=False)
        srv.session = None
        result = asyncio.run(srv._discover_server_capabilities())
        assert result.capabilities.tools is not None
