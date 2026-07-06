"""Regression tests for #724: the MCP HTTP/SSE transport must pin a brotli-free
``Accept-Encoding`` so httpx never negotiates ``br``.

With brotlicffi importable, httpx advertises ``Accept-Encoding: gzip, deflate, br``
by default; some MCP servers' streamed JSON then trips a brotlicffi decoder bug
that fails ``initialize()`` and drops the whole server's tools for the run (204
brotli DecodingErrors + downstream lazyweb CancelledErrors in the prod logs).
``_run_http`` now seeds ``accept-encoding: gzip, deflate`` on the transport
headers (before all three transport branches), respecting an explicit override.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _build_sse_server():
    from tools.mcp_tool import MCPServerTask

    server = MCPServerTask("accept-encoding-test")
    server._auth_type = ""
    server._sampling = None
    return server


@pytest.fixture
def patch_sse_client():
    """Replace ``sse_client`` with a fake that records its kwargs (incl. headers)."""
    captured: dict = {}

    class _FakeStream:
        async def __aenter__(self):
            return (AsyncMock(), AsyncMock())

        async def __aexit__(self, *a):
            return False

    def fake_sse_client(**kwargs):
        captured.clear()
        captured.update(kwargs)
        return _FakeStream()

    class _FakeSession:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            s = MagicMock()
            s.initialize = AsyncMock()
            return s

        async def __aexit__(self, *a):
            return False

    with patch("tools.mcp_tool.sse_client", new=fake_sse_client), patch(
        "tools.mcp_tool.ClientSession", new=_FakeSession
    ):
        yield captured


def _drive_run_http(server, config):
    from tools.mcp_tool import MCPServerTask

    async def drive():
        with patch.object(
            MCPServerTask, "_wait_for_lifecycle_event",
            new=AsyncMock(return_value="shutdown"),
        ), patch.object(MCPServerTask, "_discover_tools", new=AsyncMock()):
            try:
                await asyncio.wait_for(server._run_http(config), timeout=2.0)
            except (asyncio.TimeoutError, StopAsyncIteration, Exception):
                pass

    asyncio.run(drive())


def _accept_encoding(headers):
    if not headers:
        return None
    for k, v in headers.items():
        if k.lower() == "accept-encoding":
            return v
    return None


class TestAcceptEncodingPin:
    def test_default_accept_encoding_excludes_brotli(self, patch_sse_client):
        server = _build_sse_server()
        _drive_run_http(
            server,
            {"url": "https://example.com/mcp/sse", "transport": "sse", "timeout": 60},
        )
        ae = _accept_encoding(patch_sse_client.get("headers"))
        assert ae == "gzip, deflate", f"Accept-Encoding = {ae!r}"
        assert "br" not in (ae or ""), "brotli must not be negotiated (#724)"

    def test_explicit_accept_encoding_override_is_respected(self, patch_sse_client):
        server = _build_sse_server()
        _drive_run_http(
            server,
            {
                "url": "https://example.com/mcp/sse",
                "transport": "sse",
                "timeout": 60,
                "headers": {"Accept-Encoding": "identity"},
            },
        )
        assert _accept_encoding(patch_sse_client.get("headers")) == "identity"
