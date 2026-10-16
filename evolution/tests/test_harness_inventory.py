"""Tests for :mod:`evolution.lib.harness_inventory` (issue #39, slice 1).

The inventory is the "component observability" foundation for the forge
pipeline: a deterministic, file-level list of editable harness components.
Tests cover a synthetic temp checkout (fully deterministic) AND the real
repo layout (the inventory must actually find this repository's skills and
prompt assembly — a self-referential but load-bearing check).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from evolution.lib.harness_inventory import (
    HarnessComponent,
    build_harness_inventory,
    render_inventory_markdown,
)


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    """A minimal synthetic repo: two skills, prompt builder, one tool, config."""
    root = tmp_path / "repo"
    (root / "skills" / "category-a" / "alpha").mkdir(parents=True)
    (root / "skills" / "category-a" / "beta").mkdir(parents=True)
    (root / "agent").mkdir()
    (root / "tools").mkdir()
    (root / "skills" / "category-a" / "alpha" / "SKILL.md").write_text(
        "# Alpha", encoding="utf-8"
    )
    (root / "skills" / "category-a" / "beta" / "skill.md").write_text(
        "# Beta", encoding="utf-8"
    )
    (root / "agent" / "prompt_builder.py").write_text("", encoding="utf-8")
    (root / "tools" / "widget_tool.py").write_text("", encoding="utf-8")
    (root / "tools" / "__init__.py").write_text("", encoding="utf-8")
    (root / "cli-config.yaml.example").write_text("", encoding="utf-8")
    return root


class TestBuildInventory:
    def test_synthetic_repo_finds_all_kinds(self, fake_repo: Path):
        components = build_harness_inventory(fake_repo)
        by_kind = {
            kind: [c for c in components if c.kind == kind]
            for kind in ("skill", "system_prompt", "tool_prompt", "config")
        }
        # Both skills found regardless of filename case (SKILL.md / skill.md).
        assert sorted(c.component_id for c in by_kind["skill"]) == ["alpha", "beta"]
        assert [c.path for c in by_kind["system_prompt"]] == ["agent/prompt_builder.py"]
        # __init__.py is not a component.
        assert all(c.path != "tools/__init__.py" for c in by_kind["tool_prompt"])
        assert [c.path for c in by_kind["tool_prompt"]] == ["tools/widget_tool.py"]
        assert [c.path for c in by_kind["config"]] == ["cli-config.yaml.example"]

    def test_components_have_stable_ids_and_paths(self, fake_repo: Path):
        components = build_harness_inventory(fake_repo)
        ids = [c.component_id for c in components]
        assert len(ids) == len(set(ids)), "component ids must be unique"
        for c in components:
            assert isinstance(c, HarnessComponent)
            assert c.path == str(c.path) and not c.path.startswith("/")
            assert c.editable is True

    def test_missing_dirs_yield_empty_not_crash(self, tmp_path: Path):
        assert build_harness_inventory(tmp_path / "nowhere") == []

    def test_real_repo_contains_its_own_skills(self):
        """Load-bearing: the inventory must find THIS repo's harness."""
        components = build_harness_inventory()
        paths = {c.path for c in components}
        assert "agent/prompt_builder.py" in paths
        assert "cli-config.yaml.example" in paths
        # The evolution skill set is part of the harness.
        assert any(
            "skills/evolution/evolution-orchestrator/SKILL.md" == p for p in paths
        )
        assert any(p.startswith("tools/") for p in paths)


class TestRender:
    def test_render_groups_by_kind_and_counts(self, fake_repo: Path):
        components = build_harness_inventory(fake_repo)
        md = render_inventory_markdown(components, root=fake_repo)
        assert md.startswith("# Harness component inventory")
        assert "Repository root: `" in md
        assert "## skill" in md
        assert "## system_prompt" in md
        assert "## config" in md
        assert "`skills/category-a/alpha/SKILL.md`" in md
        assert f"{len(components)} editable components" in md

    def test_render_with_empty_inventory(self):
        md = render_inventory_markdown([], root=Path("/tmp"))
        assert "0 editable components" in md
