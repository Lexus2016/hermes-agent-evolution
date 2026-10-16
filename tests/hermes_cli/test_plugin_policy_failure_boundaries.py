"""Failed dependency/config reads must not become plugin installation consent."""

from pathlib import Path
from unittest.mock import Mock

import pytest


@pytest.mark.parametrize("assume_yes", [False, True])
def test_invalid_declaration_refuses_consent_even_when_preapproved(tmp_path, assume_yes):
    from hermes_cli.plugins_cmd_install import _install_plugin_python_deps

    (tmp_path / "pyproject.toml").write_text("[project\ninvalid TOML", encoding="utf-8")
    console = Mock()
    consented, reason = _install_plugin_python_deps({"name": "broken"}, tmp_path, console,
                                                   assume_yes=assume_yes)
    assert consented is False
    assert reason.failure_class == "manifest_invalid"
    assert "invalid Python dependency declaration" in reason
    console.print.assert_not_called()


def test_active_replacement_parse_failure_preserves_installed_tree_and_environment(tmp_path, monkeypatch):
    from hermes_cli.plugins_transaction import publish_plugin
    from hermes_cli import plugins_cmd
    from pm import client, workspace

    target, staged = tmp_path / "installed", tmp_path / "staged"
    target.mkdir()
    staged.mkdir()
    old = target / "__init__.py"
    old.write_text("OLD = True\n", encoding="utf-8")
    (staged / "plugin.yaml").write_text("name: broken\n", encoding="utf-8")
    malformed = staged / "pyproject.toml"
    malformed.write_text("[project\ninvalid TOML", encoding="utf-8")
    before = {p.name: p.read_bytes() for p in target.iterdir()}
    sync = Mock(side_effect=AssertionError("must not prepare or publish rejected replacement"))
    monkeypatch.setattr(client, "sync_venv", sync)
    monkeypatch.setattr(workspace, "enabled_plugin_dirs", lambda **kwargs: {target.resolve()})
    monkeypatch.setattr(plugins_cmd, "_console", Mock())
    with pytest.raises(plugins_cmd.PluginOperationError, match="installed plugin and active environment are unchanged") as error:
        publish_plugin(staged, target, {}, {}, require_consent=True)
    assert error.value.failure_class == "manifest_invalid"
    sync.assert_not_called()
    assert {p.name: p.read_bytes() for p in target.iterdir()} == before
    assert malformed.exists()


def test_unreadable_dependency_summary_warns_without_granting_consent(tmp_path):
    from hermes_cli.plugins_cmd_install import _python_dependency_summary, _install_plugin_python_deps

    (tmp_path / "pyproject.toml").write_text("[project\ninvalid TOML", encoding="utf-8")
    warnings = []
    assert _python_dependency_summary(tmp_path, warnings) == []
    assert len(warnings) == 1 and "Could not read Python dependencies" in warnings[0]
    # Empty dashboard detail never substitutes for the independent consent check.
    consented, reason = _install_plugin_python_deps({"name": "broken"}, tmp_path, Mock(), assume_yes=True)
    assert not consented and reason.failure_class == "manifest_invalid"


@pytest.mark.parametrize("read_failure", ["injected_accessor", "readonly_loader"])
def test_failed_cadence_policy_read_skips_all_work(tmp_path, monkeypatch, read_failure, caplog):
    from hermes_cli import config, plugins_cadence as cadence

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    error = Mock(side_effect=RuntimeError("private-policy-canary"))
    config_get = error
    if read_failure == "readonly_loader":
        monkeypatch.setattr(config, "load_config_readonly", error)
        config_get = None
    assert cadence.check_interval_hours(config_get) == 0
    assert cadence.auto_apply_enabled(config_get) is False
    scan, apply = Mock(), Mock()
    assert cadence.run_scheduled_check(run_checks_fn=scan, plugins_dir=tmp_path / "plugins",
                                      apply_updates_fn=apply, config_get=config_get) is None
    scan.assert_not_called()
    apply.assert_not_called()
    assert not (tmp_path / "plugin-update-checks").exists()
    messages = [r for r in caplog.records if r.name == cadence.__name__]
    assert messages and all("private-policy-canary" not in r.getMessage() and not r.exc_info for r in messages)
    assert "hermes config check" in messages[0].getMessage()
