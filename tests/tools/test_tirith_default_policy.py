"""Canonical config loads preserve the effective scanner policy of fork main365acb."""
from pathlib import Path
from unittest.mock import Mock

import pytest

from hermes_cli import config
from tools import approval, tirith_security

# Main365acb DEFAULT_CONFIG.security and scanner fallback use these exact values.
MAIN_DEFAULT_POLICY = {
    "tirith_enabled": True, "tirith_path": "tirith",
    "tirith_timeout": 5, "tirith_fail_open": True,
}


@pytest.fixture
def policy_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for key in ("TIRITH_ENABLED", "TIRITH_FAIL_OPEN", "TIRITH_TIMEOUT", "TIRITH_BIN"):
        monkeypatch.delenv(key, raising=False)
    config._LOAD_CONFIG_CACHE.clear()
    yield tmp_path
    config._LOAD_CONFIG_CACHE.clear()


@pytest.mark.parametrize("contents", [None, "security: {}\n"])
def test_canonical_default_gate_matches_main_policy_without_running_scanner(policy_home, monkeypatch, contents):
    path = policy_home / "config.yaml"
    if contents is not None:
        path.write_text(contents, encoding="utf-8")
    scan = Mock(return_value={"action": "allow", "findings": [], "summary": "synthetic"})
    monkeypatch.setattr(tirith_security, "check_command_security", scan)
    assert approval._tirith_scan("echo synthetic")["action"] == "allow"
    scan.assert_called_once_with("echo synthetic", _config=MAIN_DEFAULT_POLICY)
    assert tirith_security._load_security_config() == MAIN_DEFAULT_POLICY
    effective = config.load_config_readonly()["security"]
    assert {key: effective[key] for key in MAIN_DEFAULT_POLICY} == MAIN_DEFAULT_POLICY
    assert path.read_text(encoding="utf-8-sig") == contents if contents is not None else not path.exists()


@pytest.mark.parametrize("enabled", [True, False])
def test_canonical_explicit_policy_reaches_gate_unchanged(policy_home, monkeypatch, enabled):
    contents = (f"security:\n  tirith_enabled: {str(enabled).lower()}\n"
                "  tirith_path: /synthetic/scanner\n  tirith_timeout: 17\n  tirith_fail_open: false\n")
    path = policy_home / "config.yaml"
    path.write_text(contents, encoding="utf-8")
    expected = {"tirith_enabled": enabled, "tirith_path": "/synthetic/scanner",
                "tirith_timeout": 17, "tirith_fail_open": False}
    scan = Mock(return_value={"action": "block", "findings": [], "summary": "synthetic"})
    monkeypatch.setattr(tirith_security, "check_command_security", scan)
    result = approval._tirith_scan("echo synthetic")
    if enabled:
        scan.assert_called_once_with("echo synthetic", _config=expected)
        assert result["action"] == "block"
    else:
        scan.assert_not_called()
        assert result["action"] == "allow"
    assert tirith_security._load_security_config() == expected
    assert path.read_text(encoding="utf-8-sig") == contents
