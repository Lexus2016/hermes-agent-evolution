"""Unreadable active policy cannot silently disable scanner checks or approve commands."""
import builtins
from pathlib import Path
from unittest.mock import Mock

from hermes_cli import config

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


@pytest.fixture
def scanner(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for key in ("TIRITH_ENABLED", "TIRITH_FAIL_OPEN", "TIRITH_TIMEOUT", "TIRITH_BIN"):
        monkeypatch.delenv(key, raising=False)
    config._LOAD_CONFIG_CACHE.clear()
    monkeypatch.setattr(tirith_security, "_circuit_open", False)
    monkeypatch.setattr(tirith_security, "is_platform_supported", lambda: True)
    monkeypatch.setattr(tirith_security, "_resolve_tirith_path", lambda path: None)
    monkeypatch.setattr(approval, "_should_skip_container_guards", lambda *a, **k: False)
    monkeypatch.setattr(approval, "_floor_block", lambda *a, **k: None)
    monkeypatch.setattr("agent.terminal_approval_batch.consume_prepared_guard", lambda *a: None)
    monkeypatch.setattr(approval.approval_context, "_get_approval_mode", lambda: "manual")
    monkeypatch.setattr(approval, "_yolo_active", lambda: False)
    monkeypatch.setattr(approval, "_command_matches_permanent_allowlist", lambda command: False)
    monkeypatch.setattr(approval, "detect_dangerous_command", lambda command: (False, None, None))
    yield tmp_path
    config._LOAD_CONFIG_CACHE.clear()


@pytest.mark.parametrize("failure", ["malformed_file", "exception", "unreadable", "last_good", "shape", "marker_import", "config_import"])
@pytest.mark.parametrize("entrypoint", ["direct", "interactive", "unattended", "missing_scanner"])
def test_unreadable_policy_refuses_execution_and_approval(scanner, monkeypatch, failure, entrypoint):
    if failure == "malformed_file":
        (scanner / "config.yaml").write_text("security: [broken\n", encoding="utf-8")
    elif failure.endswith("_import"):
        original = builtins.__import__
        module = "hermes_cli.config_read_errors" if failure == "marker_import" else "hermes_cli.config"

        def unavailable(name, *args, **kwargs):
            if name == module:
                raise RuntimeError("synthetic private failure")
            return original(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", unavailable)
    else:
        def failed_config():
            if failure == "exception":
                raise RuntimeError("synthetic private failure")
            if failure == "unreadable":
                return FailedConfigRead({}, error=PermissionError("synthetic private failure"))
            if failure == "last_good":
                return FailedConfigRead({"security": {"tirith_enabled": False}}, error=ValueError("synthetic private failure"))
            return {"security": "invalid mapping"}
        monkeypatch.setattr(config, "load_config_readonly", failed_config)
    callback = Mock(side_effect=AssertionError("a missing policy is not an approval finding"))
    resolver = Mock(return_value=None)
    monkeypatch.setattr(tirith_security, "_resolve_tirith_path", resolver)
    if entrypoint == "direct":
        result = tirith_security.check_command_security("echo synthetic")
        assert result["action"] == "block"
    else:
        unattended = entrypoint == "unattended"
        monkeypatch.setattr(approval, "_presence", lambda cb: (cb, not unattended, False, False))
        if unattended:
            monkeypatch.setattr(approval, "_unattended_contexts", lambda: [approval._CRON_CTX])
            monkeypatch.setattr(approval.approval_context, "_get_cron_approval_mode", lambda: "deny")
        if entrypoint == "missing_scanner":
            scanner_import = builtins.__import__

            def unavailable(name, *args, **kwargs):
                if name == "tools.tirith_security":
                    raise ImportError("synthetic missing scanner")
                return scanner_import(name, *args, **kwargs)

            monkeypatch.setattr(builtins, "__import__", unavailable)
        result = approval.check_all_command_guards("echo synthetic", "local", approval_callback=callback)
        assert result["approved"] is False
    assert result["failure_class"] == "configuration_error"
    assert "private failure" not in str(result)
    assert tirith_security.ensure_installed() is None
    callback.assert_not_called()
    resolver.assert_not_called()


@pytest.mark.parametrize("mode", ["default", "disabled", "env_disabled", "fail_closed", "off", "yolo", "allowlist", "unattended_approve"])
def test_valid_policy_and_existing_bypass_precedence(scanner, monkeypatch, mode):
    security = {"tirith_enabled": mode != "disabled", "tirith_fail_open": mode != "fail_closed"}
    if mode == "default":
        security = {}
    load = Mock(side_effect=[{"security": security}, RuntimeError("must not reload scanner policy")])
    monkeypatch.setattr(config, "load_config_readonly", load)
    if mode == "env_disabled":
        monkeypatch.setenv("TIRITH_ENABLED", "false")
    if mode in {"off", "yolo", "allowlist", "unattended_approve"}:
        monkeypatch.setattr(approval.approval_context, "_get_approval_mode", lambda: "off" if mode == "off" else "manual")
        monkeypatch.setattr(approval, "_yolo_active", lambda: mode == "yolo")
        monkeypatch.setattr(approval, "_command_matches_permanent_allowlist", lambda command: mode == "allowlist")
        monkeypatch.setattr(approval, "_presence", lambda cb: (cb, False, False, False))
        monkeypatch.setattr(approval, "_unattended_contexts", lambda: [approval._CRON_CTX])
        monkeypatch.setattr(approval.approval_context, "_get_cron_approval_mode", lambda: "approve")
        assert approval.check_all_command_guards("echo synthetic", "local")["approved"] is True
        load.assert_not_called()
    else:
        result = approval._tirith_scan("echo synthetic")
        assert result["action"] == ("block" if mode == "fail_closed" else "allow")
        assert load.call_count == 1
        if mode == "fail_closed":
            assert "fail-closed" in result["summary"]


def test_policy_loader_diagnostic_redacts_original_failure(monkeypatch, caplog):
    caplog.set_level("DEBUG", logger="tools.tirith_config")
    from tools.tirith_config import TirithConfigurationError, read_security_policy
    def failed():
        raise RuntimeError("synthetic private value")
    monkeypatch.setattr(config, "load_config_readonly", failed)
    with pytest.raises(TirithConfigurationError) as error:
        read_security_policy()
    assert str(error.value) == "Security configuration unavailable (RuntimeError)"
    assert "synthetic private value" not in str(error.value)
    assert error.value.__cause__ is None
    assert error.value.__suppress_context__ is True
    assert "synthetic private value" not in caplog.text
    assert "RuntimeError" in caplog.text
