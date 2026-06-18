"""Tests for agent/skills_loader_v2.py — the opt-in graph-ordered loader (#298).

Covers the four behaviours the issue calls out:

  * topological ordering — dependencies (``requires`` targets) load before
    their dependents;
  * clear cycle error — a ``requires`` cycle raises a named
    :class:`SkillRequiresCycleError` rather than silently picking an order;
  * legacy / un-graphed skills fall back — a skill with no graph frontmatter is
    never dropped, it is appended in flat order;
  * flag OFF == flat behaviour — with the flag off, the load order is exactly
    the legacy ``iter_skill_index_files`` order (byte-identical).
"""

from pathlib import Path

import pytest

from agent.skill_utils import iter_skill_index_files
from agent.skills_loader_v2 import (
    CONFIG_KEY,
    ENV_FLAG,
    SkillRequiresCycleError,
    iter_skill_files_for_load,
    ordered_skill_files,
    ordered_skill_files_or_flat,
    v2_enabled,
)


# ── Helpers ─────────────────────────────────────────────────────────────────


def _write_skill(
    skills_dir: Path, category: str, name: str, graph_block: str = ""
) -> Path:
    """Write a minimal SKILL.md and return the directory it lives in."""
    d = skills_dir / category / name
    d.mkdir(parents=True, exist_ok=True)
    fm_graph = f"\n    graph:\n{graph_block}" if graph_block else ""
    (d / "SKILL.md").write_text(
        f"""---
name: {name}
description: test skill {name}
metadata:
  hermes:
    tags: [test]{fm_graph}
---

# {name}
""",
        encoding="utf-8",
    )
    return d


def _names(files, skills_dir: Path):
    """Map a list of SKILL.md paths back to their skill (directory) names."""
    return [Path(f).parent.name for f in files]


@pytest.fixture(autouse=True)
def _flag_off(monkeypatch):
    """Default every test to the v2 loader being OFF unless it opts in.

    Pins the env var so a developer's ambient HERMES_SKILLS_LOADER_V2 can't
    flip a 'flag off' assertion, and neutralises config so v2_enabled() is
    deterministic.
    """
    monkeypatch.delenv(ENV_FLAG, raising=False)
    monkeypatch.setattr(
        "agent.skill_preprocessing.load_skills_config", lambda: {}, raising=True
    )


# ── flag resolution ──────────────────────────────────────────────────────────


def test_v2_disabled_by_default():
    assert v2_enabled() is False


def test_v2_enabled_via_env(monkeypatch):
    monkeypatch.setenv(ENV_FLAG, "1")
    assert v2_enabled() is True


def test_v2_enabled_via_config(monkeypatch):
    monkeypatch.setattr(
        "agent.skill_preprocessing.load_skills_config",
        lambda: {CONFIG_KEY: True},
        raising=True,
    )
    assert v2_enabled() is True


def test_env_falsy_overrides_truthy_config(monkeypatch):
    """An explicit falsy env var wins over a truthy config value."""
    monkeypatch.setenv(ENV_FLAG, "0")
    monkeypatch.setattr(
        "agent.skill_preprocessing.load_skills_config",
        lambda: {CONFIG_KEY: True},
        raising=True,
    )
    assert v2_enabled() is False


# ── topological ordering ─────────────────────────────────────────────────────


def test_topological_order_deps_before_dependents(tmp_path):
    skills = tmp_path / "skills"
    # app requires lib; lib requires base. Load order must be base, lib, app.
    _write_skill(skills, "cat", "app", graph_block="      requires: [lib]")
    _write_skill(skills, "cat", "lib", graph_block="      requires: [base]")
    _write_skill(skills, "cat", "base")

    ordered = ordered_skill_files(skills)
    names = _names(ordered, skills)

    assert set(names) == {"app", "lib", "base"}
    assert names.index("base") < names.index("lib") < names.index("app")


def test_diamond_dependency_orders_root_first(tmp_path):
    skills = tmp_path / "skills"
    # top requires {left, right}; both require base. base must be first, top last.
    _write_skill(
        skills, "cat", "top", graph_block="      requires: [left, right]"
    )
    _write_skill(skills, "cat", "left", graph_block="      requires: [base]")
    _write_skill(skills, "cat", "right", graph_block="      requires: [base]")
    _write_skill(skills, "cat", "base")

    names = _names(ordered_skill_files(skills), skills)
    assert names[0] == "base"
    assert names[-1] == "top"
    assert names.index("base") < names.index("left")
    assert names.index("base") < names.index("right")
    assert names.index("left") < names.index("top")
    assert names.index("right") < names.index("top")


# ── clear cycle error ────────────────────────────────────────────────────────


def test_requires_cycle_raises_named_error(tmp_path):
    skills = tmp_path / "skills"
    _write_skill(skills, "cat", "a", graph_block="      requires: [b]")
    _write_skill(skills, "cat", "b", graph_block="      requires: [a]")

    with pytest.raises(SkillRequiresCycleError) as exc_info:
        ordered_skill_files(skills)

    err = exc_info.value
    # The cycle members are named in the error so an operator can fix it.
    assert err.cycles, "cycle list must be populated"
    cycle_members = {n for cycle in err.cycles for n in cycle}
    assert {"a", "b"} <= cycle_members
    assert "a" in str(err) and "b" in str(err)


def test_or_flat_falls_back_on_cycle(tmp_path):
    """The convenience wrapper degrades to flat order on a cycle, never raising."""
    skills = tmp_path / "skills"
    _write_skill(skills, "cat", "a", graph_block="      requires: [b]")
    _write_skill(skills, "cat", "b", graph_block="      requires: [a]")
    _write_skill(skills, "cat", "lonely")

    ordered = ordered_skill_files_or_flat(skills)
    flat = list(iter_skill_index_files(skills, "SKILL.md"))
    # Cycle -> no reordering attempted; we get exactly the flat scan.
    assert ordered == flat


# ── legacy / un-graphed fallback ─────────────────────────────────────────────


def test_ungraphed_skill_is_never_dropped(tmp_path):
    skills = tmp_path / "skills"
    _write_skill(skills, "cat", "app", graph_block="      requires: [lib]")
    _write_skill(skills, "cat", "lib")
    # legacy: a skill with NO graph frontmatter at all.
    _write_skill(skills, "cat", "legacy")

    ordered = ordered_skill_files(skills)
    names = _names(ordered, skills)

    # Same set of files as the flat scan — nothing added, nothing dropped.
    assert set(names) == {"app", "lib", "legacy"}
    assert len(ordered) == len(set(ordered)) == 3
    # Graph constraint still honoured among graphed skills.
    assert names.index("lib") < names.index("app")
    # The un-graphed skill is still present.
    assert "legacy" in names


def test_returned_set_equals_flat_set(tmp_path):
    """ordered_skill_files is a pure permutation of the flat scan."""
    skills = tmp_path / "skills"
    _write_skill(skills, "a", "one", graph_block="      requires: [two]")
    _write_skill(skills, "a", "two")
    _write_skill(skills, "b", "three")
    _write_skill(skills, "b", "four", graph_block="      composes-with: [three]")

    ordered = set(ordered_skill_files(skills))
    flat = set(iter_skill_index_files(skills, "SKILL.md"))
    assert ordered == flat


def test_empty_dir_returns_empty(tmp_path):
    skills = tmp_path / "skills"
    skills.mkdir()
    assert ordered_skill_files(skills) == []
    assert ordered_skill_files_or_flat(skills) == []


# ── flag OFF == flat behaviour (byte-identical) ──────────────────────────────


def test_flag_off_is_flat_order(tmp_path):
    skills = tmp_path / "skills"
    # Deliberately set up edges that WOULD reorder under v2, to prove the flag
    # off path ignores the graph entirely.
    _write_skill(skills, "cat", "app", graph_block="      requires: [lib]")
    _write_skill(skills, "cat", "lib")

    flat = list(iter_skill_index_files(skills, "SKILL.md"))
    # Flag is off (autouse fixture) -> identical to the legacy scan.
    assert iter_skill_files_for_load(skills) == flat


def test_flag_on_reorders(tmp_path, monkeypatch):
    skills = tmp_path / "skills"
    _write_skill(skills, "cat", "app", graph_block="      requires: [lib]")
    _write_skill(skills, "cat", "lib")

    monkeypatch.setenv(ENV_FLAG, "1")
    loaded = iter_skill_files_for_load(skills)
    names = _names(loaded, skills)
    # Under v2 the dependency loads first.
    assert names.index("lib") < names.index("app")


def test_flag_on_cycle_falls_back_to_flat(tmp_path, monkeypatch):
    """End-to-end: flag on + cycle -> entry point still returns flat, no raise."""
    skills = tmp_path / "skills"
    _write_skill(skills, "cat", "a", graph_block="      requires: [b]")
    _write_skill(skills, "cat", "b", graph_block="      requires: [a]")

    monkeypatch.setenv(ENV_FLAG, "1")
    flat = list(iter_skill_index_files(skills, "SKILL.md"))
    assert iter_skill_files_for_load(skills) == flat
