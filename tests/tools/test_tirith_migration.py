"""Configured scanner compatibility survives the upstream plugin migration."""
import builtins
import sys
from types import SimpleNamespace

import pytest

from tools import approval


@pytest.mark.parametrize("security", [{}, {"tirith_enabled": False, "tirith_fail_open": False}])
def test_unconfigured_or_disabled_scanner_is_not_imported(monkeypatch, security):
    monkeypatch.delenv("TIRITH_ENABLED", raising=False)
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {"security": security})
    original = builtins.__import__
    def guarded(name, *args, **kwargs):
        assert name != "tools.tirith_security"
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", guarded)
    assert approval._tirith_scan("echo safe")["action"] == "allow"


def test_configured_scanner_verdict_survives_plugin_migration(monkeypatch):
    monkeypatch.delenv("TIRITH_ENABLED", raising=False)
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {"security": {"tirith_enabled": True}})
    verdict = {"action": "block", "summary": "unsafe URL", "findings": []}
    monkeypatch.setitem(sys.modules, "tools.tirith_security", SimpleNamespace(check_command_security=lambda command: verdict))
    assert approval._tirith_scan("echo unsafe") == verdict


def test_tirith_always_choice_is_session_only(monkeypatch):
    sessions = []
    monkeypatch.setattr(approval, "approve_session", lambda session, key: sessions.append((session, key)))
    monkeypatch.setattr(approval, "approve_permanent", lambda key: pytest.fail("Scanner verdict must not be permanently allowlisted"))
    approval._persist_choice("session", "always", ["tirith:unsafe-url"])
    assert sessions == [("session", "tirith:unsafe-url")]
