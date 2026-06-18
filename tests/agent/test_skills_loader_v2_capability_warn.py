"""Load-time capability-conflict warning in skills_loader_v2 (#299).

The capability *surface* and conflict *detection* already shipped (PR #309:
``SkillGraph.capability_surface`` + ``GraphValidation.capability_conflicts``,
exposed via the ``skill_relationships`` tool).  This is the remaining slice:
when the opt-in v2 loader builds the graph, a capability provided by two or
more skills is surfaced as a **load-time WARNING** (a log line).

These tests cover the three behaviours the issue calls out:

  * conflict -> a warning is emitted (one per ambiguous capability, naming the
    capability and every provider);
  * no conflict -> silent (no capability-conflict warning);
  * flag OFF -> silent (the v2 path never runs, so nothing is logged) — the
    warning is strictly gated behind ``v2_enabled()``.
"""

import logging
from pathlib import Path

from agent.skills_loader_v2 import (
    ENV_FLAG,
    iter_skill_files_for_load,
    ordered_skill_files,
)

# Substring every capability-conflict warning carries — lets the tests assert
# on the governance warning specifically, not on incidental log noise.
_WARN_MARK = "capability conflict"


# ── Helpers ─────────────────────────────────────────────────────────────────


def _write_skill(
    skills_dir: Path, category: str, name: str, provides: str = ""
) -> Path:
    """Write a minimal SKILL.md declaring ``provides`` capabilities (optional)."""
    d = skills_dir / category / name
    d.mkdir(parents=True, exist_ok=True)
    graph_block = (
        f"\n    graph:\n      provides: [{provides}]" if provides else ""
    )
    (d / "SKILL.md").write_text(
        f"""---
name: {name}
description: test skill {name}
metadata:
  hermes:
    tags: [test]{graph_block}
---

# {name}
""",
        encoding="utf-8",
    )
    return d


def _conflict_warnings(caplog) -> list:
    """The WARNING records emitted by the loader for a capability conflict."""
    return [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING
        and r.name == "agent.skills_loader_v2"
        and _WARN_MARK in r.getMessage()
    ]


# ── conflict -> warning emitted ──────────────────────────────────────────────


def test_capability_conflict_emits_warning(tmp_path, caplog):
    skills = tmp_path / "skills"
    # Two skills both provide the same capability `search` -> ambiguous.
    _write_skill(skills, "cat", "alpha", provides="search")
    _write_skill(skills, "cat", "beta", provides="search")

    with caplog.at_level(logging.WARNING, logger="agent.skills_loader_v2"):
        ordered_skill_files(skills)

    warnings = _conflict_warnings(caplog)
    assert len(warnings) == 1, "exactly one ambiguous capability -> one warning"
    msg = warnings[0].getMessage()
    # The warning names the capability and every provider so an operator can act.
    assert "search" in msg
    assert "alpha" in msg and "beta" in msg


def test_multiple_conflicts_each_warn(tmp_path, caplog):
    skills = tmp_path / "skills"
    _write_skill(skills, "cat", "a1", provides="cap-x")
    _write_skill(skills, "cat", "a2", provides="cap-x")
    _write_skill(skills, "cat", "b1", provides="cap-y")
    _write_skill(skills, "cat", "b2", provides="cap-y")

    with caplog.at_level(logging.WARNING, logger="agent.skills_loader_v2"):
        ordered_skill_files(skills)

    warnings = _conflict_warnings(caplog)
    assert len(warnings) == 2
    joined = " ".join(w.getMessage() for w in warnings)
    assert "cap-x" in joined and "cap-y" in joined


def test_warning_does_not_change_returned_files(tmp_path, caplog):
    """The warning is advisory — it must not add/drop any skill file."""
    skills = tmp_path / "skills"
    _write_skill(skills, "cat", "alpha", provides="search")
    _write_skill(skills, "cat", "beta", provides="search")

    with caplog.at_level(logging.WARNING, logger="agent.skills_loader_v2"):
        ordered = ordered_skill_files(skills)

    names = sorted(Path(f).parent.name for f in ordered)
    assert names == ["alpha", "beta"]


# ── no conflict -> silent ────────────────────────────────────────────────────


def test_no_conflict_is_silent(tmp_path, caplog):
    skills = tmp_path / "skills"
    # Distinct capabilities -> no overlap -> no warning.
    _write_skill(skills, "cat", "alpha", provides="search")
    _write_skill(skills, "cat", "beta", provides="export")

    with caplog.at_level(logging.WARNING, logger="agent.skills_loader_v2"):
        ordered_skill_files(skills)

    assert _conflict_warnings(caplog) == []


def test_no_provides_at_all_is_silent(tmp_path, caplog):
    skills = tmp_path / "skills"
    # Skills with no `provides` declaration -> no capabilities -> no conflict.
    _write_skill(skills, "cat", "alpha")
    _write_skill(skills, "cat", "beta")

    with caplog.at_level(logging.WARNING, logger="agent.skills_loader_v2"):
        ordered_skill_files(skills)

    assert _conflict_warnings(caplog) == []


# ── flag OFF -> silent (gated) ───────────────────────────────────────────────


def test_flag_off_is_silent_even_with_conflict(tmp_path, caplog, monkeypatch):
    """With v2 off, the loader never runs the graph -> no capability warning."""
    monkeypatch.delenv(ENV_FLAG, raising=False)
    monkeypatch.setattr(
        "agent.skill_preprocessing.load_skills_config", lambda: {}, raising=True
    )
    skills = tmp_path / "skills"
    _write_skill(skills, "cat", "alpha", provides="search")
    _write_skill(skills, "cat", "beta", provides="search")

    with caplog.at_level(logging.WARNING, logger="agent.skills_loader_v2"):
        # The public entry point honours the flag; OFF -> flat scan, no graph.
        iter_skill_files_for_load(skills)

    assert _conflict_warnings(caplog) == []


def test_flag_on_via_env_warns(tmp_path, caplog, monkeypatch):
    """End-to-end through the entry point: flag ON surfaces the warning."""
    monkeypatch.setenv(ENV_FLAG, "1")
    skills = tmp_path / "skills"
    _write_skill(skills, "cat", "alpha", provides="search")
    _write_skill(skills, "cat", "beta", provides="search")

    with caplog.at_level(logging.WARNING, logger="agent.skills_loader_v2"):
        iter_skill_files_for_load(skills)

    warnings = _conflict_warnings(caplog)
    assert len(warnings) == 1
    assert "search" in warnings[0].getMessage()
