# -*- coding: utf-8 -*-
"""Unit tests for tools.mcp_discover (#3247)."""

import time
import pytest
from tools.mcp_discover import (
    MCPCapabilityCache,
    discover_server_capabilities,
    is_mcp_capability_supported,
    parse_discover_response,
)


class TestMCPDiscover:
    def test_parse_discover_response(self):
        resp = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "protocolVersion": "2026-07-28",
                "capabilities": {
                    "tools": {"listChanged": True},
                    "resources": {"subscribe": True},
                    "prompts": {},
                    "logging": {},
                },
                "stateless": True,
            },
        }
        caps = parse_discover_response(resp)
        assert caps["tools"] == {"listChanged": True}
        assert caps["resources"] == {"subscribe": True}
        assert caps["stateless"] is True
        assert caps["protocol_version"] == "2026-07-28"

    def test_cache_set_and_get(self):
        cache = MCPCapabilityCache(default_ttl=10.0)
        cache.set("https://mcp.example.com", {"tools": True, "logging": False})
        assert cache.supports("https://mcp.example.com", "tools") is True
        assert cache.supports("https://mcp.example.com", "logging") is False
        assert cache.supports("https://mcp.example.com", "sampling") is False
        assert cache.supports("https://unknown.server", "tools") is None

    def test_cache_ttl_expiry(self):
        cache = MCPCapabilityCache(default_ttl=0.1)
        cache.set("server1", {"tools": True}, ttl_seconds=0.05)
        assert cache.get("server1") == {"tools": True}
        time.sleep(0.06)
        assert cache.get("server1") is None

    def test_discover_server_capabilities_integration(self):
        server = "https://api.github.mcp.com"
        raw_resp = {
            "capabilities": {
                "tools": True,
                "prompts": True,
            }
        }
        caps = discover_server_capabilities(server, raw_resp)
        assert caps["tools"] is True
        assert is_mcp_capability_supported(server, "tools") is True
        assert is_mcp_capability_supported(server, "resources") is False
