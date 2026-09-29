from pathlib import Path
from unittest.mock import MagicMock






def test_fresh_config_runs_auto_prune_at_startup(monkeypatch, tmp_path: Path):
    """A config.yaml with NO ``sessions:`` keys must reach the prune call with the
    new defaults (the loader deep-merges DEFAULT_CONFIG)."""
    import cli
    import hermes_cli.config
    import hermes_constants
    from hermes_cli.config import DEFAULT_CONFIG

    session_db = MagicMock()
    session_db.get_meta.return_value = "already-done"
    # Simulate load_config() on a fresh home: only defaults for the section.
    monkeypatch.setattr(
        hermes_cli.config,
        "load_config",
        lambda: {"sessions": dict(DEFAULT_CONFIG["sessions"])},
    )
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)

    cli._run_state_db_auto_maintenance(session_db)

    session_db.maybe_auto_prune_and_vacuum.assert_called_once_with(
        retention_days=90,
        min_interval_hours=24,
        min_vacuum_interval_days=30,
        vacuum=True,
        sessions_dir=tmp_path / "sessions",
        db_size_vacuum_threshold=768 * 1024 * 1024,
    )


def test_explicit_auto_prune_false_is_respected(monkeypatch, tmp_path: Path):
    """Migration guard: an install that explicitly opted out keeps its choice."""
    import cli
    import hermes_cli.config
    import hermes_constants

    session_db = MagicMock()
    session_db.get_meta.return_value = "already-done"
    monkeypatch.setattr(
        hermes_cli.config,
        "load_config",
        lambda: {"sessions": {"auto_prune": False, "retention_days": 90}},
    )
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)

    cli._run_state_db_auto_maintenance(session_db)

    session_db.maybe_auto_prune_and_vacuum.assert_not_called()






def test_negative_retention_days_in_config_deletes_nothing(monkeypatch, tmp_path: Path):
    """E2E through the real startup path: ``sessions.retention_days: -1`` reaches
    ``maybe_auto_prune_and_vacuum`` from this config loader; without validation it
    builds a future cutoff and deletes every ended session."""
    import cli
    import hermes_cli.config
    import hermes_constants
    from hermes_state import SessionDB

    session_db = SessionDB(db_path=tmp_path / "state.db")
    try:
        session_db.create_session(session_id="ended", source="cli")
        session_db.end_session("ended", "done")
        monkeypatch.setattr(
            hermes_cli.config,
            "load_config",
            lambda: {"sessions": {"auto_prune": True, "retention_days": -1}},
        )
        monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)

        cli._run_state_db_auto_maintenance(session_db)

        assert session_db.get_session("ended") is not None
    finally:
        session_db.close()


def test_cli_auto_maintenance_forwards_vacuum_interval(monkeypatch, tmp_path: Path):
    import cli
    import hermes_cli.config
    import hermes_constants

    session_db = MagicMock()
    session_db.get_meta.return_value = "already-done"
    monkeypatch.setattr(
        hermes_cli.config,
        "load_config",
        lambda: {
            "sessions": {
                "auto_prune": True,
                "retention_days": 90,
                "vacuum_after_prune": True,
                "min_interval_hours": 24,
                "min_vacuum_interval_days": 17,
            }
        },
    )
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)

    cli._run_state_db_auto_maintenance(session_db)

    session_db.maybe_auto_prune_and_vacuum.assert_called_once_with(
        retention_days=90,
        min_interval_hours=24,
        min_vacuum_interval_days=17,
        vacuum=True,
        sessions_dir=tmp_path / "sessions",
        db_size_vacuum_threshold=768 * 1024 * 1024,
    )
