"""Tests for the temporal validity-window tracking in tools/skill_usage.py (#2700).

Skills are invalidated, not deleted: demotion closes the record's validity
window, restore archives the closed window into validity_history and opens a
fresh one, and point-in-time queries reconstruct past states.
"""

import json
from pathlib import Path

import pytest


@pytest.fixture
def skills_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "skills").mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    import importlib
    import tools.skill_usage as mod
    importlib.reload(mod)
    monkeypatch.setattr(mod, "_prune_builtins_enabled", lambda: False)
    return home


def _seed_usage(skills_dir: Path, records: dict) -> None:
    (skills_dir / ".usage.json").write_text(
        json.dumps(records, indent=1), encoding="utf-8"
    )


def test_fresh_record_has_open_window(skills_home):
    from tools.skill_usage import record_created, skill_validity

    record_created("my-skill", agent_created=True)
    window = skill_validity("my-skill")
    assert window["valid_from"] is not None
    assert window["valid_to"] is None
    assert window["invalid_at"] is None
    assert window["state"] == "active"


def test_demotion_closes_window_without_deleting(skills_home):
    from tools.skill_usage import (
        STATE_STALE,
        get_record,
        record_created,
        set_state,
        skill_validity,
    )

    record_created("my-skill", agent_created=True)
    set_state("my-skill", STATE_STALE)
    window = skill_validity("my-skill")
    assert window["valid_to"] is not None
    assert window["invalid_at"] is not None
    assert window["invalidation_reason"] == "demoted:stale"
    # Invalidate-not-delete: the record itself survives demotion.
    assert get_record("my-skill") != {}


def test_restore_reopens_window_and_keeps_history(skills_home):
    from tools.skill_usage import (
        STATE_ACTIVE,
        STATE_STALE,
        record_created,
        set_state,
        skill_validity,
        skill_validity_history,
    )

    record_created("my-skill", agent_created=True)
    set_state("my-skill", STATE_STALE)
    set_state("my-skill", STATE_ACTIVE)

    window = skill_validity("my-skill")
    assert window["valid_to"] is None
    assert window["invalidation_reason"] is None

    history = skill_validity_history("my-skill")
    assert len(history) == 1
    assert history[0]["valid_to"] is not None
    assert history[0]["reason"] == "demoted:stale"
    # The new window opens no earlier than the previous one closed.
    assert window["valid_from"] >= history[0]["valid_to"]


def test_explicit_reason_is_recorded(skills_home):
    from tools.skill_usage import (
        STATE_ARCHIVED,
        record_created,
        set_state,
        skill_validity,
    )

    record_created("my-skill", agent_created=True)
    set_state("my-skill", STATE_ARCHIVED, reason="consolidated into reader")
    window = skill_validity("my-skill")
    assert window["invalidation_reason"] == "consolidated into reader"


def test_point_in_time_query_across_windows(skills_home):
    from tools.skill_usage import skill_state_at

    skills_dir = skills_home / "skills"
    _seed_usage(skills_dir, {
        "legacy": {
            "created_by": "agent",
            "state": "active",
            "created_at": "2026-01-01T00:00:00+00:00",
            # Historical window: Jan 1 → Feb 1 (consolidated away).
            # Current window: Feb 10 → Mar 1 (demoted since).
            "valid_from": "2026-02-10T00:00:00+00:00",
            "valid_to": "2026-03-01T00:00:00+00:00",
            "invalid_at": "2026-03-01T00:00:00+00:00",
            "invalidation_reason": "demoted:stale",
            "validity_history": [
                {
                    "valid_from": "2026-01-01T00:00:00+00:00",
                    "valid_to": "2026-02-01T00:00:00+00:00",
                    "reason": "consolidated into core",
                }
            ],
        }
    })
    # Inside the first (historical) window.
    assert skill_state_at("legacy", "2026-01-15T00:00:00+00:00") == "valid"
    # In the gap between the closed historical window and the current one.
    assert skill_state_at("legacy", "2026-02-05T00:00:00+00:00") == "invalid"
    # Inside the current (closed) window.
    assert skill_state_at("legacy", "2026-02-15T00:00:00+00:00") == "valid"
    # After the final close.
    assert skill_state_at("legacy", "2026-03-15T00:00:00+00:00") == "invalid"
    # Before any recorded validity.
    assert skill_state_at("legacy", "2025-12-31T00:00:00+00:00") == "invalid"
    # Unknown skill / unparseable instant.
    assert skill_state_at("missing", "2026-01-15T00:00:00+00:00") == "unknown"
    assert skill_state_at("legacy", "not-a-timestamp") == "unknown"


def test_legacy_record_backfills_valid_from(skills_home):
    from tools.skill_usage import skill_validity

    skills_dir = skills_home / "skills"
    _seed_usage(skills_dir, {
        "old-skill": {
            "created_by": "agent",
            "state": "active",
            "created_at": "2026-04-28T00:00:00+00:00",
        }
    })
    window = skill_validity("old-skill")
    assert window["valid_from"] == "2026-04-28T00:00:00+00:00"
    assert window["valid_to"] is None
