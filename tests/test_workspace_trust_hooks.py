#!/usr/bin/env python3
"""Unit tests for workspace-trust gate on hook registration (#1389).

Trust model under test:

* ``bundled`` — trusted (ships in the repo).
* ``user``    — trusted only when the plugin genuinely resolves inside
                ``<HERMES_HOME>/plugins/``.
* ``project`` / ``entrypoint`` / unknown — untrusted; need an explicit
                ``allow_hooks`` opt-in keyed by ``<source>:<plugin_id>``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest


def _make_ctx(source: str = "user", key: str = "test-plugin", path=None):
    """Build a PluginContext bypassing heavy __init__ deps."""
    from hermes_cli.plugins import PluginContext

    ctx = PluginContext.__new__(PluginContext)
    ctx.manifest = SimpleNamespace(
        name=key, key=key, source=source, kind="standalone", path=path
    )
    ctx._manager = SimpleNamespace(_hooks={})
    return ctx


@pytest.fixture
def profile_plugin(tmp_path, monkeypatch):
    """A real plugin directory inside a temporary HERMES_HOME."""
    hermes_home = tmp_path / "hermes_home"
    plugin_dir = hermes_home / "plugins" / "test-plugin"
    plugin_dir.mkdir(parents=True)
    monkeypatch.setattr("hermes_cli.plugins.get_hermes_home", lambda: hermes_home)
    return plugin_dir


class TestHookTrustGate:
    def test_bundled_source_trusted_by_default(self):
        ctx = _make_ctx(source="bundled")
        assert ctx._hook_trust_allowed() is True

    def test_user_source_in_profile_dir_trusted_by_default(self, profile_plugin):
        """The profile directory is trusted per #1389 — no extra flag needed."""
        ctx = _make_ctx(source="user", path=str(profile_plugin))
        assert ctx._hook_trust_allowed() is True

    def test_project_source_untrusted_by_default(self):
        with patch("hermes_cli.config.load_config", return_value={}):
            ctx = _make_ctx(source="project", path="/some/repo/.hermes/plugins/p")
            assert ctx._hook_trust_allowed() is False

    def test_entrypoint_source_untrusted_by_default(self):
        """pip install is not consent to silent tool-call interception."""
        with patch("hermes_cli.config.load_config", return_value={}):
            ctx = _make_ctx(source="entrypoint")
            assert ctx._hook_trust_allowed() is False

    def test_project_source_trusted_with_source_keyed_config(self):
        cfg = {"plugins": {"entries": {"project:test-plugin": {"allow_hooks": True}}}}
        with patch("hermes_cli.config.load_config", return_value=cfg):
            ctx = _make_ctx(source="project")
            assert ctx._hook_trust_allowed() is True

    def test_bare_plugin_id_key_does_not_grant_trust(self):
        """A bare id key must not approve hooks — it enables name shadowing.

        Keying on the plugin name alone would let a project plugin that takes
        the name of an approved plugin inherit that approval.
        """
        cfg = {"plugins": {"entries": {"test-plugin": {"allow_hooks": True}}}}
        with patch("hermes_cli.config.load_config", return_value=cfg):
            ctx = _make_ctx(source="project")
            assert ctx._hook_trust_allowed() is False

    def test_approval_for_one_source_does_not_leak_to_another(self):
        """``entrypoint:x`` approval must not cover ``project:x``."""
        cfg = {
            "plugins": {"entries": {"entrypoint:test-plugin": {"allow_hooks": True}}}
        }
        with patch("hermes_cli.config.load_config", return_value=cfg):
            assert _make_ctx(source="entrypoint")._hook_trust_allowed() is True
            assert _make_ctx(source="project")._hook_trust_allowed() is False

    def test_symlink_out_of_profile_dir_is_not_trusted(self, tmp_path, monkeypatch):
        """A symlink planted in the profile dir must not inherit its trust.

        #1389 names "a symlinked directory" as an attack vector: the scanner
        labels it source="user", but the code lives outside the profile root.
        """
        hermes_home = tmp_path / "hermes_home"
        (hermes_home / "plugins").mkdir(parents=True)
        outside = tmp_path / "untrusted_elsewhere"
        outside.mkdir()
        link = hermes_home / "plugins" / "evil"
        link.symlink_to(outside, target_is_directory=True)
        monkeypatch.setattr("hermes_cli.plugins.get_hermes_home", lambda: hermes_home)
        with patch("hermes_cli.config.load_config", return_value={}):
            ctx = _make_ctx(source="user", key="evil", path=str(link))
            assert ctx._hook_trust_allowed() is False

    def test_user_source_without_path_is_not_trusted(self):
        """No path means containment can't be proven — fail closed."""
        with patch("hermes_cli.config.load_config", return_value={}):
            ctx = _make_ctx(source="user", path=None)
            assert ctx._hook_trust_allowed() is False

    def test_config_load_failure_fails_closed(self):
        with patch("hermes_cli.config.load_config", side_effect=RuntimeError("boom")):
            ctx = _make_ctx(source="project")
            assert ctx._hook_trust_allowed() is False


class TestRegisterHookGate:
    def test_bundled_registers_hook(self):
        ctx = _make_ctx(source="bundled")
        ctx.register_hook("pre_tool_call", lambda **kw: None)
        assert "pre_tool_call" in ctx._manager._hooks

    def test_profile_plugin_registers_hook(self, profile_plugin):
        ctx = _make_ctx(source="user", path=str(profile_plugin))
        ctx.register_hook("pre_tool_call", lambda **kw: None)
        assert "pre_tool_call" in ctx._manager._hooks

    def test_project_blocked_without_config(self):
        with patch("hermes_cli.config.load_config", return_value={}):
            ctx = _make_ctx(source="project")
            ctx.register_hook("pre_tool_call", lambda **kw: None)
            assert "pre_tool_call" not in ctx._manager._hooks

    def test_project_registers_with_config(self):
        cfg = {"plugins": {"entries": {"project:test-plugin": {"allow_hooks": True}}}}
        with patch("hermes_cli.config.load_config", return_value=cfg):
            ctx = _make_ctx(source="project")
            ctx.register_hook("pre_tool_call", lambda **kw: None)
            assert "pre_tool_call" in ctx._manager._hooks

    def test_blocked_hook_logs_actionable_config_key(self, caplog):
        """The warning must name the exact source-qualified key to add."""
        with patch("hermes_cli.config.load_config", return_value={}):
            ctx = _make_ctx(source="project")
            with caplog.at_level("WARNING"):
                ctx.register_hook("pre_tool_call", lambda **kw: None)
        assert "project:test-plugin" in caplog.text

    def test_accepted_hook_is_audit_logged(self, caplog):
        """#1389 requirement 3: log hook registrations with source + trust level."""
        ctx = _make_ctx(source="bundled")
        with caplog.at_level("INFO"):
            ctx.register_hook("pre_tool_call", lambda **kw: None)
        assert "Hook registered" in caplog.text
        assert "source='bundled'" in caplog.text
