"""Unreadable active policy cannot silently disable scanner checks or approve commands."""
from unittest.mock import Mock

import pytest

from hermes_cli.config_read_errors import FailedConfigRead
from tools import approval, tirith_security


@pytest.fixture(autouse=True)
def scanner_env(monkeypatch):
    for key in ("TIRITH_ENABLED", "TIRITH_FAIL_OPEN", "TIRITH_TIMEOUT", "TIRITH_BIN"):
        monkeypatch.delenv(key, raising=False)


def failed_config(kind):
    if kind == "exception":
        raise RuntimeError("synthetic loader failure")
    if kind == "malformed":
        return FailedConfigRead({}, error=ValueError("synthetic YAML"))
    if kind == "unreadable":
        return FailedConfigRead({}, error=PermissionError("synthetic denied"))
    if kind == "last_good":
        return FailedConfigRead({"security": {"tirith_enabled": True, "tirith_fail_open": False}},
                                error=ValueError("synthetic YAML"))
    return {"security": "invalid mapping"}


@pytest.mark.parametrize("kind", ["exception", "malformed", "unreadable", "last_good", "shape"])
@pytest.mark.parametrize("enabled", [None, "true", "false"])
def test_failed_policy_is_explicit_even_with_environment_override(monkeypatch, kind, enabled):
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: failed_config(kind))
    if enabled is not None:
        monkeypatch.setenv("TIRITH_ENABLED", enabled)
    scan = Mock(side_effect=AssertionError("must not scan unresolved policy"))
    monkeypatch.setattr(tirith_security, "check_command_security", scan)
    result = approval._tirith_scan("echo synthetic")
    assert result["action"] == "block"
    assert result["configuration_error"] is True
    assert result["failure_class"] == "configuration_error"
    scan.assert_not_called()


@pytest.mark.parametrize("security", [{}, {"tirith_enabled": False}])
@pytest.mark.parametrize("enabled", [None, "false"])
def test_valid_missing_or_disabled_policy_still_allows(monkeypatch, security, enabled):
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {"security": security})
    if enabled is not None:
        monkeypatch.setenv("TIRITH_ENABLED", enabled)
    monkeypatch.setattr(tirith_security, "check_command_security", Mock(side_effect=AssertionError("disabled scanner")))
    assert approval._tirith_scan("echo synthetic")["action"] == "allow"


@pytest.mark.parametrize("enabled", [None, "true"])
def test_enabled_scan_uses_one_validated_config_without_lossy_reload(monkeypatch, enabled):
    if enabled is not None:
        monkeypatch.setenv("TIRITH_ENABLED", enabled)
    load = Mock(side_effect=[{"security": {"tirith_enabled": True, "tirith_fail_open": False}},
                            RuntimeError("second read must not happen")])
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", load)
    monkeypatch.setattr(tirith_security, "_circuit_open", False)
    monkeypatch.setattr(tirith_security, "is_platform_supported", lambda: True)
    monkeypatch.setattr(tirith_security, "_resolve_tirith_path", lambda path: None)
    result = approval._tirith_scan("echo synthetic")
    assert result["action"] != "allow"
    assert "fail-closed" in result["summary"]
    assert load.call_count == 1


def test_direct_scanner_refuses_config_failure_before_resolver(monkeypatch):
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: failed_config("unreadable"))
    monkeypatch.setattr(tirith_security, "_resolve_tirith_path", Mock(side_effect=AssertionError("must not resolve")))
    result = tirith_security.check_command_security("echo synthetic")
    assert result["configuration_error"] is True
    assert result["action"] == "block"


def test_approval_entrypoint_refuses_error_without_prompt_or_execution(monkeypatch):
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: failed_config("malformed"))
    monkeypatch.setattr(approval, "_should_skip_container_guards", lambda *a, **k: False)
    monkeypatch.setattr(approval, "_floor_block", lambda *a, **k: None)
    monkeypatch.setattr("agent.terminal_approval_batch.consume_prepared_guard", lambda *a: None)
    monkeypatch.setattr(approval.approval_context, "_get_approval_mode", lambda: "manual")
    monkeypatch.setattr(approval, "_yolo_active", lambda: False)
    monkeypatch.setattr(approval, "_command_matches_permanent_allowlist", lambda command: False)
    callback = Mock(side_effect=AssertionError("policy error must not be user-overridden"))
    monkeypatch.setattr(approval, "_presence", lambda cb: (cb, True, False, False))
    execute = Mock()
    decision = approval.check_all_command_guards("echo synthetic", "local", approval_callback=callback)
    if decision["approved"]:
        execute()
    assert decision["approved"] is False
    assert decision["failure_class"] == "configuration_error"
    callback.assert_not_called()
    execute.assert_not_called()


@pytest.mark.parametrize("bypass", ["off", "yolo", "allowlist"])
def test_existing_explicit_bypass_precedence_is_unchanged(monkeypatch, bypass):
    monkeypatch.setattr(approval, "_should_skip_container_guards", lambda *a, **k: False)
    monkeypatch.setattr(approval, "_floor_block", lambda *a, **k: None)
    monkeypatch.setattr("agent.terminal_approval_batch.consume_prepared_guard", lambda *a: None)
    monkeypatch.setattr(approval.approval_context, "_get_approval_mode", lambda: "off" if bypass == "off" else "manual")
    monkeypatch.setattr(approval, "_yolo_active", lambda: bypass == "yolo")
    monkeypatch.setattr(approval, "_command_matches_permanent_allowlist", lambda command: bypass == "allowlist")
    monkeypatch.setattr(approval, "_tirith_scan", Mock(side_effect=AssertionError("existing bypass moved")))
    assert approval.check_all_command_guards("echo synthetic", "local")["approved"] is True


def test_valid_enabled_policy_honors_explicit_environment_disable(monkeypatch):
    monkeypatch.setenv("TIRITH_ENABLED", "false")
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {
        "security": {"tirith_enabled": True, "tirith_fail_open": False}})
    monkeypatch.setattr(tirith_security, "check_command_security", Mock(side_effect=AssertionError("disabled scanner")))
    assert approval._tirith_scan("echo synthetic")["action"] == "allow"
