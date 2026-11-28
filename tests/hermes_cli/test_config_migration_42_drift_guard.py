"""Upstream's reserved v42 must preserve this fork's active cron model-drift guard (#44585)."""

import pytest
import hermes_yaml as yaml


@pytest.mark.parametrize("stamp", [None, 41, 43])
@pytest.mark.parametrize("guard", [False, True])
def test_full_migration_preserves_explicit_model_drift_guard(tmp_path, monkeypatch, stamp, guard):
    from hermes_cli.config import migrate_config, read_raw_config, load_config_readonly

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    cron = {"model_drift_guard": guard, "model_drift_auto_repin": not guard, "max_parallel_jobs": 2}
    config = {"cron": cron}
    if stamp is not None:
        config["_config_version"] = stamp
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    results = migrate_config(interactive=False, quiet=True)
    raw = read_raw_config()
    assert raw["cron"] == cron
    effective = load_config_readonly()["cron"]
    assert effective["model_drift_guard"] is guard
    assert effective["model_drift_auto_repin"] is (not guard)
    assert not any("model_drift_guard" in note for note in results["config_added"])
    settled = path.read_bytes()
    assert migrate_config(interactive=False, quiet=True)["config_added"] == []
    assert path.read_bytes() == settled


def test_guard_resolver_reads_the_surviving_value(tmp_path, monkeypatch):
    from hermes_cli.config import migrate_config, load_config_readonly
    from hermes_cli.config import cron_model_drift_guard_enabled

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"_config_version": 41, "cron": {"model_drift_guard": False}}),
                    encoding="utf-8")
    migrate_config(interactive=False, quiet=True)
    assert cron_model_drift_guard_enabled(load_config_readonly()) is False
