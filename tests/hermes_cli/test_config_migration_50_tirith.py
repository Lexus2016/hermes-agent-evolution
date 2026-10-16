"""Upstream's reserved v50 must preserve this fork's active Tirith policy."""

import pytest
import hermes_yaml as yaml


@pytest.mark.parametrize("stamp", [None, 49, 51])
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("fail_open", [False, True])
def test_full_migration_preserves_explicit_tirith_policy(tmp_path, monkeypatch, stamp, enabled, fail_open):
    from hermes_cli.config import migrate_config, read_raw_config, load_config_readonly

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    security = {
        "tirith_enabled": enabled, "tirith_fail_open": fail_open,
        "tirith_path": "/synthetic/scanner", "tirith_timeout": 9, "redact_secrets": True,
    }
    config = {
        "security": security,
        "mcp_servers": {"old": {"command": "x", "enabled": True, "disabled": True}},
        "cron": {"bot_chat_delivery_timeout_seconds": 900, "max_parallel_jobs": 2},
    }
    if stamp is not None:
        config["_config_version"] = stamp
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    results = migrate_config(interactive=False, quiet=True)
    raw = read_raw_config()
    assert raw["security"] == security
    effective = load_config_readonly()["security"]
    assert all(effective[key] == value for key, value in security.items())
    assert raw["mcp_servers"]["old"] == {"command": "x", "enabled": False}
    assert raw["cron"]["max_parallel_jobs"] == 2
    if stamp is None or stamp < 51:
        assert "bot_chat_delivery_timeout_seconds" not in raw["cron"]
    assert not any("tirith" in note for note in results["config_added"])
    settled = path.read_bytes()
    assert migrate_config(interactive=False, quiet=True)["config_added"] == []
    assert path.read_bytes() == settled


def test_authored_default_values_survive_other_migration_writes(tmp_path, monkeypatch):
    from hermes_cli.config import migrate_config, read_raw_config

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    security = {"tirith_enabled": True, "tirith_path": "tirith", "tirith_timeout": 5, "tirith_fail_open": True}
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"_config_version": 49, "security": security,
                                   "cron": {"bot_chat_delivery_timeout_seconds": 900}}), encoding="utf-8")
    migrate_config(interactive=False, quiet=True)
    assert read_raw_config()["security"] == security


def test_fresh_profile_effective_policy_keeps_scanner_enabled(tmp_path, monkeypatch):
    from hermes_cli.config import load_config_readonly

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    security = load_config_readonly()["security"]
    assert security["tirith_enabled"] is True
    assert security["tirith_path"] == "tirith"
    assert security["tirith_timeout"] == 5
    assert security["tirith_fail_open"] is True
    assert not (tmp_path / "config.yaml").exists()
