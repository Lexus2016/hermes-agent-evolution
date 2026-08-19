"""Config migration v38 — legacy toolset-name rewrite (messaging → hermes-*)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import yaml  # noqa: E402

from hermes_cli.config_migrations import _migrate_to_38  # noqa: E402


def _write_cfg(tmp_path, tools):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump({"_config_version": 37, "tools": tools}))
    return cfg


def test_messaging_rewritten_to_platform_toolset(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    cfg = _write_cfg(tmp_path, {
        "cli": {"enabled": ["messaging", "terminal"]},
        "telegram": {"enabled": ["messaging"]},
    })
    results = {"config_added": []}
    _migrate_to_38(results, quiet=True)
    out = yaml.safe_load(cfg.read_text())
    assert out["tools"]["cli"]["enabled"] == ["hermes-cli", "terminal"]
    assert out["tools"]["telegram"]["enabled"] == ["hermes-telegram"]


def test_unknown_names_dropped_valid_kept(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    cfg = _write_cfg(tmp_path, {
        "google_chat": {"enabled": ["hermes-google_chat", "spotify"]},
        "teams": {"disabled": ["hermes-teams", "vision"]},
    })
    _migrate_to_38({"config_added": []}, quiet=True)
    out = yaml.safe_load(cfg.read_text())
    assert out["tools"]["google_chat"]["enabled"] == ["spotify"]
    assert out["tools"]["teams"]["disabled"] == ["vision"]


def test_valid_config_untouched_and_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    cfg = _write_cfg(tmp_path, {"telegram": {"enabled": ["hermes-telegram"]}})
    before = cfg.read_text()
    results = {"config_added": []}
    _migrate_to_38(results, quiet=True)
    assert cfg.read_text() == before
    assert results["config_added"] == []
