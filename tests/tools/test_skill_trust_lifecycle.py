"""Tests for the trust lifecycle in tools/skill_usage.py (#2256)."""

import json
from pathlib import Path

import pytest


def _write_skill(skills_dir: Path, name: str) -> None:
    d = skills_dir / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\n---\n# {name}\n", encoding="utf-8")


@pytest.fixture
def skills_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with a clean skills/ dir for each test."""
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


def test_provisional_promoted_after_threshold(skills_home):
    """A provisional skill is promoted to trusted after N successful uses."""
    from tools.skill_usage import (
        TRUST_PROVISIONAL, TRUST_TRUSTED, get_trust_state, record_skill_outcome,
    )
    skills_dir = skills_home / "skills"
    _write_skill(skills_dir, "mine")
    from tools.skill_usage import mark_agent_created
    mark_agent_created("mine")

    assert get_trust_state("mine") == TRUST_PROVISIONAL
    # 2 successes — still provisional (threshold is 3).
    record_skill_outcome("mine", success=True)
    record_skill_outcome("mine", success=True)
    assert get_trust_state("mine") == TRUST_PROVISIONAL
    # 3rd success promotes.
    record_skill_outcome("mine", success=True)
    assert get_trust_state("mine") == TRUST_TRUSTED


def test_failure_resets_promotion_counter(skills_home):
    """A failure resets the promotion counter so the run of successes breaks."""
    from tools.skill_usage import (
        TRUST_PROVISIONAL, get_trust_state, record_skill_outcome,
    )
    skills_dir = skills_home / "skills"
    _write_skill(skills_dir, "mine")
    from tools.skill_usage import mark_agent_created
    mark_agent_created("mine")

    record_skill_outcome("mine", success=True)
    record_skill_outcome("mine", success=True)
    record_skill_outcome("mine", success=False)  # breaks the run
    record_skill_outcome("mine", success=True)
    record_skill_outcome("mine", success=True)
    # Only 2 consecutive successes after the failure — still provisional.
    assert get_trust_state("mine") == TRUST_PROVISIONAL


def test_trusted_demoted_on_high_failure_rate(skills_home, monkeypatch):
    """A trusted skill is demoted when its recent failure rate crosses the threshold."""
    from tools.skill_usage import (
        TRUST_PROVISIONAL, TRUST_TRUSTED, get_trust_state, record_skill_outcome,
        set_trust_state,
    )
    skills_dir = skills_home / "skills"
    _write_skill(skills_dir, "mine")
    from tools.skill_usage import mark_agent_created
    mark_agent_created("mine")

    # Force it trusted and lower the demotion min-outcomes so the test is quick.
    assert set_trust_state("mine", TRUST_TRUSTED)
    import tools.skill_usage as mod
    monkeypatch.setattr(mod, "_TRUST_DEMOTION_MIN_OUTCOMES", 2)
    monkeypatch.setattr(mod, "_trust_demotion_failure_rate", lambda: 0.5)

    # 2 failures out of 4 outcomes -> rate 0.5 >= threshold -> demote.
    record_skill_outcome("mine", success=True)
    record_skill_outcome("mine", success=True)
    record_skill_outcome("mine", success=False)
    record_skill_outcome("mine", success=False)
    assert get_trust_state("mine") == TRUST_PROVISIONAL


def test_set_trust_state_invalid_rejected(skills_home):
    """set_trust_state rejects invalid states and returns False."""
    from tools.skill_usage import set_trust_state
    assert set_trust_state("mine", "bogus") is False


def test_get_trust_state_missing_returns_provisional(skills_home):
    """A missing/corrupt record returns provisional, never raises."""
    from tools.skill_usage import TRUST_PROVISIONAL, get_trust_state
    assert get_trust_state("does-not-exist") == TRUST_PROVISIONAL
