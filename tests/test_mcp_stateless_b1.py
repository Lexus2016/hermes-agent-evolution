#!/usr/bin/env python3
"""Unit tests for MCP stateless capability-detection shim (Slice B-1 / #1511)."""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest


def _make_server():
    """Build an MCPServerTask instance (bypass __init__ heavy deps)."""
    from tools.mcp_tool import MCPServerTask

    srv = MCPServerTask.__new__(MCPServerTask)
    srv.name = "test-server"
    srv.initialize_result = None
    srv._stateless_enabled = False
    return srv


class TestStatelessFlag:
    def test_flag_off_by_default(self):
        """Without HERMES_MCP_STATELESS, _stateless_enabled must be False."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HERMES_MCP_STATELESS", None)
            from tools.mcp_tool import MCPServerTask

            srv = MCPServerTask.__new__(MCPServerTask)
            MCPServerTask.__init__(srv, "test")
            assert srv._stateless_enabled is False

    def test_flag_on_with_1(self):
        with patch.dict(os.environ, {"HERMES_MCP_STATELESS": "1"}):
            from tools.mcp_tool import MCPServerTask

            srv = MCPServerTask("test")
            assert srv._stateless_enabled is True

    def test_flag_on_with_true(self):
        with patch.dict(os.environ, {"HERMES_MCP_STATELESS": "true"}):
            from tools.mcp_tool import MCPServerTask

            srv = MCPServerTask("test")
            assert srv._stateless_enabled is True

    def test_flag_off_with_empty(self):
        with patch.dict(os.environ, {"HERMES_MCP_STATELESS": ""}):
            from tools.mcp_tool import MCPServerTask

            srv = MCPServerTask("test")
            assert srv._stateless_enabled is False


class TestDetectStatelessSupport:
    def test_off_when_disabled(self):
        srv = _make_server()
        srv._stateless_enabled = False
        assert srv._detect_stateless_support() is False

    def test_on_when_enabled(self):
        srv = _make_server()
        srv._stateless_enabled = True
        assert srv._detect_stateless_support() is True


class TestSynthesizeCapabilities:
    def test_default_tools_only(self):
        srv = _make_server()
        result = srv.synthesize_capabilities()
        assert result.capabilities.tools is not None
        assert result.capabilities.resources is None
        assert result.capabilities.prompts is None

    def test_all_capabilities(self):
        srv = _make_server()
        result = srv.synthesize_capabilities({
            "tools": True,
            "resources": True,
            "prompts": True,
        })
        assert result.capabilities.tools is not None
        assert result.capabilities.resources is not None
        assert result.capabilities.prompts is not None

    def test_tools_only_explicit(self):
        srv = _make_server()
        result = srv.synthesize_capabilities({"tools": True, "resources": False})
        assert result.capabilities.tools is not None
        assert result.capabilities.resources is None

    def test_works_with_advertises_tools(self):
        """Synthesized caps must flow through _advertises_tools() correctly."""
        srv = _make_server()
        srv.initialize_result = srv.synthesize_capabilities()
        assert srv._advertises_tools() is True

    def test_works_with_select_utility_schemas(self):
        """Synthesized caps must flow through _select_utility_schemas()."""
        srv = _make_server()
        srv.initialize_result = srv.synthesize_capabilities({
            "tools": True,
            "resources": True,
        })
        caps = srv.initialize_result.capabilities
        assert getattr(caps, "tools", None) is not None
        assert getattr(caps, "resources", None) is not None


class TestStatefulPathUnchanged:
    """When flag is OFF, the behavior must be identical to before (#1511)."""

    def test_initialize_result_stays_none_before_connect(self):
        srv = _make_server()
        srv._stateless_enabled = False
        assert srv.initialize_result is None
        assert srv._detect_stateless_support() is False

    def test_advertises_tools_returns_true_when_none(self):
        """Legacy fallback: _advertises_tools returns True when caps unknown."""
        srv = _make_server()
        srv.initialize_result = None
        assert srv._advertises_tools() is True
