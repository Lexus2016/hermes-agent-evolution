"""Harness component observability — file-level inventory (issue #39, slice 1).

The Agentic Harness Engineering research behind #39 starts from one premise:
a harness (the system prompt + skills + tool prompts an agent runs on) can
only be evolved safely when its components are observable — i.e. every
editable component has a *file-level* representation, so edits are clean,
diffable, and revertible at the granularity of a single file.

This module is that first slice: it inventories the editable harness
components of THIS repository as file-level entries. Component kinds:

* ``skill`` — skill definitions (``skills/**/SKILL.md``), the largest class
  of harness components and the primary forge edit target;
* ``system_prompt`` — system-prompt assembly code (``agent/prompt_builder.py``),
  where the prompt sections the forge rewrites are composed;
* ``tool_prompt`` — tool modules (``tools/*.py``) that ship tool schemas and
  descriptions, i.e. the prompts the model sees for each tool;
* ``config`` — the CLI config schema (``cli-config.yaml.example``), the
  config surface the harness runs under.

Each entry is ``(component_id, kind, path, title, editable)`` — a stable,
file-level handle a future forge pipeline can diff, edit, and revert at
single-file granularity (the "component observability" pillar of #39).

The real consumer is the ``evolution/harness_inventory.py`` CLI (same
convention as ``evolution/detect_mode.py``): it renders the manifest as
markdown so a human — or a future forge cycle — can see exactly which files
constitute the editable harness.

Stdlib-only and import-safe.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

__all__ = [
    "COMPONENT_KINDS",
    "HarnessComponent",
    "build_harness_inventory",
    "render_inventory_markdown",
]

#: The component kinds the inventory recognizes, in render order.
COMPONENT_KINDS = ("skill", "system_prompt", "tool_prompt", "config")

#: Relative (to the repo root) paths of non-skill harness components.
#: ``*`` is expanded via sorted glob; the value is the human title.
_NON_SKILL_COMPONENTS = (
    ("system_prompt", "agent/prompt_builder.py", "System prompt assembly"),
    ("tool_prompt", "tools/*.py", "Tool prompt modules"),
    ("config", "cli-config.yaml.example", "CLI config schema"),
)

#: Repo root: two levels up from ``evolution/lib/``.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


@dataclass(frozen=True)
class HarnessComponent:
    """One file-level harness component."""

    component_id: str
    kind: str
    #: Path relative to the repo root (forward slashes).
    path: str
    title: str
    editable: bool = True


def _find_skill_files(skills_dir: Path) -> list[Path]:
    """Return skill definition files under *skills_dir*.

    The convention is ``<category>/<skill-name>/SKILL.md``. We match
    ``SKILL.md`` exactly and ALSO any file whose name lowercases to
    ``skill.md`` (union, deduped) — the inventory must never miss a skill
    just because of a filename case difference.
    """
    if not skills_dir.is_dir():
        return []
    exact = list(skills_dir.rglob("SKILL.md"))
    case_variants = [
        p
        for p in skills_dir.rglob("*")
        if p.is_file() and p.name.lower() == "skill.md" and p not in exact
    ]
    return sorted(exact + case_variants)


def build_harness_inventory(root: Optional[Path] = None) -> list[HarnessComponent]:
    """Build the file-level harness inventory for the repo at *root*.

    Defaults to this repository's root (discovered from the module path).
    Deterministic ordering: skills sorted by path, then the fixed non-skill
    components in ``COMPONENT_KINDS`` order. Missing paths are skipped, so a
    partial checkout still yields a valid (possibly empty) inventory.
    """
    base = Path(root).resolve() if root is not None else _REPO_ROOT
    components: list[HarnessComponent] = []

    skills_dir = base / "skills"
    for skill_file in _find_skill_files(skills_dir):
        rel = skill_file.relative_to(base)
        # component_id: the skill's stable name, e.g. "evolution-orchestrator".
        component_id = skill_file.parent.name
        components.append(
            HarnessComponent(
                component_id=component_id,
                kind="skill",
                path=rel.as_posix(),
                title=f"Skill: {component_id}",
            )
        )

    for kind, pattern, title in _NON_SKILL_COMPONENTS:
        if "*" in pattern:
            matches = sorted(m for m in base.glob(pattern) if m.name != "__init__.py")
        else:
            candidate = base / pattern
            matches = [candidate] if candidate.is_file() else []
        for match in matches:
            rel = match.relative_to(base)
            components.append(
                HarnessComponent(
                    component_id=rel.as_posix().replace("/", ".").replace(".py", ""),
                    kind=kind,
                    path=rel.as_posix(),
                    title=title,
                )
            )
    return components


def render_inventory_markdown(
    components: list[HarnessComponent], *, root: Optional[Path] = None
) -> str:
    """Render the inventory as a human-readable markdown manifest.

    Groups components by kind (in ``COMPONENT_KINDS`` order) and lists each
    as ``- ``kind``: ``path`` — title``. The root is printed as a header so
    the manifest is self-describing when saved to a file.
    """
    base = Path(root).resolve() if root is not None else _REPO_ROOT
    lines = [
        "# Harness component inventory",
        "",
        f"Repository root: `{base}`",
        "",
        f"{len(components)} editable components",
        "",
    ]
    for kind in COMPONENT_KINDS:
        of_kind = [c for c in components if c.kind == kind]
        if not of_kind:
            continue
        lines.append(f"## {kind}")
        lines.append("")
        for component in of_kind:
            lines.append(f"- `{component.path}` — {component.title}")
        lines.append("")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entry point: print the harness inventory as markdown.

    Usage: ``python evolution/harness_inventory.py [ROOT]`` — ROOT defaults
    to the repository root. Exit code 0 on success.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    root = Path(argv[0]).resolve() if argv else None
    components = build_harness_inventory(root)
    print(render_inventory_markdown(components, root=root))
    return 0


if __name__ == "__main__":
    sys.exit(main())
