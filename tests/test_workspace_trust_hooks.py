#!/usr/bin/env python3
"""Unit tests for workspace-trust gate on hook registration (#1389)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest


def _make_ctx(source: str = "user", key: str = "test-plugin", config=None):
    """Build a PluginContext bypassing heavy __init__ deps."""
    from hermes_cli.plugins import PluginContext

    ctx = PluginContext.__new__(PluginContext)
    ctx.manifest = SimpleNamespace(
        name=key, key=key, source=source, kind="standalone"
    )
    ctx._manager = SimpleNamespace(_hooks={})
    return ctx


class TestHookTrustGate:
    def test_bundled_source_trusted_by_default(self):
        ctx = _make_ctx(source="bundled")
        assert ctx._hook_trust_allowed() is True

    def test_user_source_untrusted_by_default(self):
        with patch("hermes_cli.config.load_config", return_value={}):
            ctx = _make_ctx(source="user")
            assert ctx._hook_trust_allowed() is False

    def test_project_source_untrusted_by_default(self):
        with patch("hermes_cli.config.load_config", return_value={}):
            ctx = _make_ctx(source="project")
            assert ctx._hook_trust_allowed() is False

    def test_user_source_trusted_with_config(self):
        cfg = {"plugins": {"entries": {"test-plugin": {"allow_hooks": True}}}}
        with patch("hermes_cli.config.load_config", return_value=cfg):
            ctx = _make_ctx(source="user")
            assert ctx._hook_trust_allowed() is True

    def test_user_source_blocked_without_allow_hooks(self):
        cfg = {"plugins": {"entries": {"test-plugin": {"allow_hooks": False}}}}
        with patch("hermes_cli.config.load_config", return_value=cfg):
            ctx = _make_ctx(source="user")
            assert ctx._hook_trust_allowed() is False


class TestRegisterHookGate:
    def test_bundled_registers_hook(self):
        ctx = _make_ctx(source="bundled")
        ctx.register_hook("pre_tool_call", lambda **kw: None)
        assert "pre_tool_call" in ctx._manager._hooks

    def test_user_blocked_without_config(self):
        with patch("hermes_cli.config.load_config", return_value={}):
            ctx = _make_ctx(source="user")
            ctx.register_hook("pre_tool_call", lambda **kw: None)
            assert "pre_tool_call" not in ctx._manager._hooks

    def test_user_registers_with_config(self):
        cfg = {"plugins": {"entries": {"test-plugin": {"allow_hooks": True}}}}
        with patch("hermes_cli.config.load_config", return_value=cfg):
            ctx = _make_ctx(source="user")
            ctx.register_hook("pre_tool_call", lambda **kw: None)
            assert "pre_tool_call" in ctx._manager._hooks
