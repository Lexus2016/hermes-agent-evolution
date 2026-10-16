"""MCP discovery coordination and redirect credential boundaries."""
from unittest.mock import patch

import pytest


class TestMCPDiscoveryCrossProcessLock:
    """Tests for the cross-process MCP discovery guard in discover_mcp_tools()."""

    @pytest.fixture(autouse=True)
    def _fast_retries(self):
        """Override retry constants so tests are fast."""
        from tools import mcp_tool
        orig_max = mcp_tool._MCP_DISCOVERY_LOCK_MAX_RETRIES
        orig_delay = mcp_tool._MCP_DISCOVERY_LOCK_RETRY_DELAY_S
        mcp_tool._MCP_DISCOVERY_LOCK_MAX_RETRIES = 3
        mcp_tool._MCP_DISCOVERY_LOCK_RETRY_DELAY_S = 0.01
        yield
        mcp_tool._MCP_DISCOVERY_LOCK_MAX_RETRIES = orig_max
        mcp_tool._MCP_DISCOVERY_LOCK_RETRY_DELAY_S = orig_delay

    def test_lock_acquired_path(self, tmp_path):
        """Lock acquired -> discovery runs normally, lock released at end."""
        from tools.mcp_tool_loop import _LockCookie
        from tools.mcp_tool_discovery import discover_mcp_tools

        lock_file = tmp_path / ".mcp-discovery.lock"
        fh = open(lock_file, "w", encoding="utf-8")
        cookie = _LockCookie(fh)

        def mock_acquire():
            return cookie

        mock_config = {"test_srv": {"command": "echo", "enabled": True}}
        with patch.object(cookie, "release", wraps=cookie.release) as release_spy:
            with patch("tools.mcp_tool_loop._try_acquire_mcp_discovery_lock", mock_acquire), \
                 patch("tools.mcp_tool._MCP_AVAILABLE", True), \
                 patch("tools.mcp_tool_config._load_mcp_config", return_value=mock_config), \
                 patch("tools.mcp_tool_discovery.register_mcp_servers", return_value=["mcp__test_srv__ping"]):
                result = discover_mcp_tools()
            assert result == ["mcp__test_srv__ping"]
            release_spy.assert_called_once()

    def test_lock_held_retries_exhausted_fallback(self):
        """All retry attempts see lock held -> runs discovery unguarded."""
        from tools.mcp_tool_discovery import discover_mcp_tools

        mock_config = {"test_srv": {"command": "echo", "enabled": True}}
        # Every attempt returns None (lock held)
        with patch("tools.mcp_tool_loop._try_acquire_mcp_discovery_lock", return_value=None), \
             patch("tools.mcp_tool._MCP_AVAILABLE", True), \
             patch("tools.mcp_tool_config._load_mcp_config", return_value=mock_config), \
             patch("tools.mcp_tool_discovery.register_mcp_servers") as reg_spy, \
             patch("tools.mcp_tool_registration._existing_tool_names", return_value=[]):
            discover_mcp_tools()
        # Must still run local discovery
        reg_spy.assert_called_once_with(mock_config)



class TestRedirectHeaderStripper:
    """Cross-origin redirect header boundary (portable Agent Plugins v1).

    The stripper is an ``AsyncClient`` factory overriding ``_build_redirect_request`` on each built
    client — the only
    seam that sees the actual redirect follow-up (``response.next_request`` is unset when response
    hooks fire) without also touching non-redirect traffic (a request hook would strip the OAuth
    auth flow's token/registration calls to a different-origin authorization server)."""

    def _build_redirect(self, httpx, *, strict=False, configured=frozenset(),
                        headers, location):
        from tools.mcp_tool_errors import _make_redirect_header_stripper

        build_client = _make_redirect_header_stripper(
            httpx, httpx.URL("https://origin.example.test/mcp"),
            strict=strict, configured_header_names=configured)
        client = build_client()
        request = httpx.Request("GET", "https://origin.example.test/mcp", headers=headers)
        response = httpx.Response(302, headers={"location": location}, request=request)
        return client._build_redirect_request(request, response)

    def test_default_strips_only_authorization(self):
        import httpx

        next_request = self._build_redirect(
            httpx, headers={"Authorization": "Bearer x", "X-Tenant": "t"},
            location="https://other.example.test/mcp")
        assert "authorization" not in next_request.headers
        assert next_request.headers["x-tenant"] == "t"

    def test_strict_strips_configured_headers_cross_origin(self):
        import httpx

        next_request = self._build_redirect(
            httpx, strict=True, configured={"x-tenant"},
            headers={"Authorization": "Bearer x", "X-Tenant": "t", "Accept": "a"},
            location="https://other.example.test/mcp")
        assert "authorization" not in next_request.headers
        assert "x-tenant" not in next_request.headers
        # Client-generated headers unrelated to package config survive.
        assert next_request.headers["accept"] == "a"

    def test_same_origin_redirect_keeps_headers(self):
        import httpx

        next_request = self._build_redirect(
            httpx, strict=True, configured={"x-tenant"},
            headers={"Authorization": "Bearer x", "X-Tenant": "t"},
            location="https://origin.example.test/other")
        assert next_request.headers["authorization"] == "Bearer x"
        assert next_request.headers["x-tenant"] == "t"
