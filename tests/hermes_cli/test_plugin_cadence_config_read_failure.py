"""Actual canonical config-read failures must not authorize plugin network checks."""

import builtins
from pathlib import Path
from unittest.mock import Mock

import pytest


@pytest.mark.parametrize("case", ["absent", "disabled", "corrupt", "corrupt_lkg", "unreadable", "stat_denied"])
def test_canonical_policy_read_boundary(tmp_path, monkeypatch, caplog, case):
    from hermes_cli import config, plugins_cadence as cadence
    from hermes_cli.config_read_errors import FailedConfigRead
    from pm import receipt

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    path = tmp_path / "config.yaml"
    if case != "absent":
        path.write_text("plugins:\n  auto_update_check_hours: 0\n  auto_apply: true\n", encoding="utf-8")
    if case == "corrupt_lkg":
        path.write_text("plugins:\n  auto_update_check_hours: 24\n  auto_apply: true\n", encoding="utf-8")
        assert config.load_config_readonly()["plugins"]["auto_update_check_hours"] == 24
    if case in {"corrupt", "corrupt_lkg"}:
        path.write_text("plugins: [private-policy-canary\n", encoding="utf-8")
    original = path.read_bytes() if path.exists() else None
    real_open, real_stat = builtins.open, Path.stat
    with monkeypatch.context() as failing:
        if case == "unreadable":
            def guarded_open(file, *args, **kwargs):
                if not isinstance(file, int) and Path(file) == path:
                    raise PermissionError("private-policy-canary")
                return real_open(file, *args, **kwargs)
            failing.setattr(builtins, "open", guarded_open)
        if case == "stat_denied":
            def guarded_stat(self, *args, **kwargs):
                if self == path:
                    raise PermissionError("private-policy-canary")
                return real_stat(self, *args, **kwargs)
            failing.setattr(Path, "stat", guarded_stat)
        if case in {"corrupt", "corrupt_lkg", "unreadable"}:
            assert isinstance(config.load_config_readonly(), FailedConfigRead)
        scan, apply = Mock(return_value=[]), Mock()
        begin = Mock(side_effect=AssertionError("a refused check cannot write a receipt"))
        if case != "absent":
            failing.setattr(receipt, "begin", begin)
        result = cadence.run_scheduled_check(run_checks_fn=scan, plugins_dir=tmp_path / "plugins",
                                             apply_updates_fn=apply)
        if case == "absent":
            assert result == [] and scan.call_count == 1
        else:
            assert result is None
            scan.assert_not_called()
            begin.assert_not_called()
            assert not (tmp_path / "plugin-update-checks").exists()
        apply.assert_not_called()
    assert (path.read_bytes() if path.exists() else None) == original
    records = [r for r in caplog.records if r.name == cadence.__name__]
    if case in {"corrupt", "corrupt_lkg", "unreadable", "stat_denied"}:
        assert records and "hermes config check" in records[0].getMessage()
        assert all("private-policy-canary" not in r.getMessage() and not r.exc_info for r in records)
        # Repair is observed without a stale failed-check marker delaying recovery.
        path.write_text("plugins:\n  auto_update_check_hours: 24\n  auto_apply: false\n", encoding="utf-8")
        recovered = Mock(return_value=[])
        assert cadence.run_scheduled_check(run_checks_fn=recovered, plugins_dir=tmp_path / "plugins",
                                           apply_updates_fn=apply) == []
        recovered.assert_called_once()
        apply.assert_not_called()
