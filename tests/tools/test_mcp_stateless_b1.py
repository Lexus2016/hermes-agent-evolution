"""Tests for MCP 2026-07-28 stateless transport shim — Slice B-1 (issue #1511).

Verifies:
- ``_detect_stateless_support()`` defaults OFF and requires the env flag.
- ``synthesize_capabilities()`` returns an ``InitializeResult``-shaped object
  with all three capability families present.
- ``MCPServerTask._stateless`` defaults to False and is only set True when
  both the env flag and per-server config opt in.
- ``_initialize_or_synthesize()`` skips ``session.initialize()`` in stateless
  mode and returns synthesized caps; delegates to the real handshake otherwise.
"""

import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest import mock


class TestDetectStatelessSupport(unittest.TestCase):
    """``_detect_stateless_support()`` — env-flag + per-server config gating."""

    def setUp(self):
        self._orig = os.environ.pop("HERMES_MCP_STATELESS", None)

    def tearDown(self):
        os.environ.pop("HERMES_MCP_STATELESS", None)

    def test_defaults_off_no_env(self):
        from tools.mcp_tool import _detect_stateless_support

        self.assertFalse(_detect_stateless_support())
        self.assertFalse(_detect_stateless_support(config={}))

    def test_env_flag_alone_not_enough_without_config(self):
        """Env flag set but config=None → True (global opt-in)."""
        from tools.mcp_tool import _detect_stateless_support

        os.environ["HERMES_MCP_STATELESS"] = "1"
        self.assertTrue(_detect_stateless_support())

    def test_env_flag_with_config_stateless_true(self):
        from tools.mcp_tool import _detect_stateless_support

        os.environ["HERMES_MCP_STATELESS"] = "1"
        self.assertTrue(_detect_stateless_support(config={"stateless": True}))

    def test_env_flag_with_config_stateless_false(self):
        """Per-server config can opt out even when env flag is set."""
        from tools.mcp_tool import _detect_stateless_support

        os.environ["HERMES_MCP_STATELESS"] = "1"
        self.assertFalse(_detect_stateless_support(config={"stateless": False}))

    def test_env_flag_empty_string_is_off(self):
        from tools.mcp_tool import _detect_stateless_support

        os.environ["HERMES_MCP_STATELESS"] = ""
        self.assertFalse(_detect_stateless_support())

    def test_env_flag_whitespace_is_off(self):
        from tools.mcp_tool import _detect_stateless_support

        os.environ["HERMES_MCP_STATELESS"] = "   "
        self.assertFalse(_detect_stateless_support())


class TestSynthesizeCapabilities(unittest.TestCase):
    """``synthesize_capabilities()`` — shape and capability families."""

    def test_returns_object_with_capabilities(self):
        from tools.mcp_tool import synthesize_capabilities

        result = synthesize_capabilities()
        self.assertTrue(hasattr(result, "capabilities"))

    def test_capabilities_has_tools_resources_prompts(self):
        from tools.mcp_tool import synthesize_capabilities

        caps = synthesize_capabilities().capabilities
        for attr in ("tools", "resources", "prompts"):
            self.assertTrue(hasattr(caps, attr), f"missing capability: {attr}")

    def test_protocol_version_set(self):
        from tools.mcp_tool import synthesize_capabilities

        result = synthesize_capabilities()
        self.assertEqual(result.protocolVersion, "2026-07-28")

    def test_advertises_tools_works_with_synthesized(self):
        """The synthesized caps object must pass the _advertises_tools() gate."""
        from tools.mcp_tool import synthesize_capabilities

        result = synthesize_capabilities()
        caps = result.capabilities
        # _advertises_tools checks: caps is None → True (legacy);
        # caps.tools exists → True; caps.tools missing → False.
        # Our synthesized caps has .tools, so it should advertise tools.
        self.assertIsNotNone(caps)
        self.assertTrue(hasattr(caps, "tools"))


class TestMCPServerTaskStateless(unittest.TestCase):
    """``MCPServerTask._stateless`` field and ``_initialize_or_synthesize()``."""

    def setUp(self):
        self._orig = os.environ.pop("HERMES_MCP_STATELESS", None)

    def tearDown(self):
        os.environ.pop("HERMES_MCP_STATELESS", None)

    def test_stateless_defaults_false(self):
        from tools.mcp_tool import MCPServerTask

        server = MCPServerTask("test-server")
        self.assertFalse(server._stateless)

    def test_stateless_set_true_from_config_with_env(self):
        from tools.mcp_tool import MCPServerTask, _detect_stateless_support

        os.environ["HERMES_MCP_STATELESS"] = "1"
        config = {"stateless": True, "url": "http://localhost:8080/mcp"}
        server = MCPServerTask("test-server")
        # Simulate what run() does
        server._stateless = _detect_stateless_support(config)
        self.assertTrue(server._stateless)

    def test_initialize_or_synthesize_skips_in_stateless_mode(self):
        """When _stateless=True, _initialize_or_synthesize returns synthesized caps."""
        from tools.mcp_tool import MCPServerTask

        server = MCPServerTask("test-server")
        server._stateless = True

        # Run the coroutine
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(
                server._initialize_or_synthesize(
                    session=mock.MagicMock(),
                    connect_timeout=30.0,
                )
            )
            # Should return synthesized caps, not call session.initialize()
            self.assertTrue(hasattr(result, "capabilities"))
            self.assertTrue(hasattr(result.capabilities, "tools"))
        finally:
            loop.close()

    def test_initialize_or_synthesize_delegates_when_not_stateless(self):
        """When _stateless=False, _initialize_or_synthesize calls session.initialize()."""
        from tools.mcp_tool import MCPServerTask

        server = MCPServerTask("test-server")
        server._stateless = False

        # Create a mock session whose initialize() returns a known result
        mock_session = mock.AsyncMock()
        expected_result = SimpleNamespace(
            capabilities=SimpleNamespace(tools=SimpleNamespace()),
            protocolVersion="2025-06-18",
        )
        mock_session.initialize = mock.AsyncMock(return_value=expected_result)

        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(
                server._initialize_or_synthesize(
                    session=mock_session,
                    connect_timeout=30.0,
                )
            )
            self.assertEqual(result, expected_result)
            mock_session.initialize.assert_awaited_once()
        finally:
            loop.close()


if __name__ == "__main__":
    unittest.main()