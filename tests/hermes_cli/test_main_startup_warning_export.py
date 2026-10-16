"""The fork's startup-warning compatibility export resolves without running it."""

from unittest.mock import Mock


def test_main_startup_warning_export_resolves_lazily(monkeypatch):
    from hermes_cli import main, update_cmd, update_cmd_fleet

    name = "_warn_pending_fleet_restart_on_startup"
    assert getattr(update_cmd, name) is getattr(update_cmd_fleet, name)
    warning = Mock(name="startup_warning")
    monkeypatch.setattr(update_cmd, name, warning)

    # Restore any pre-existing lazy cache after testing its first resolution.
    monkeypatch.setitem(vars(main), name, None)
    vars(main).pop(name)

    exported = getattr(main, name)
    assert exported is warning
    assert vars(main)[name] is warning
    warning.assert_not_called()

    exported()
    warning.assert_called_once_with()
