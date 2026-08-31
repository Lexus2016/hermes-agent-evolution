#!/usr/bin/env python3
"""Validate skills/ SKILL.md files against the open Agent Skills spec (#125).

Agent Skills (agentskills.io, originated from Anthropic) is an open standard:
*a skill is a folder containing a SKILL.md file* with YAML frontmatter that at
minimum declares ``name`` and ``description``. Hermes skills are the core
self-improvement substrate (curator-authored, heavily used by the evolution
pipeline), so malformed skill manifests must fail fast in CI rather than break
portability or discovery tooling downstream.

This checker enforces the spec's hard requirements, with Hermes-specific
metadata confined to the documented extension namespace (``metadata.hermes``):

  REQUIRED  - ``name`` present, <= 64 chars, matches its folder name
  REQUIRED  - ``description`` present, <= 1024 chars
  SPEC      - folder is ``skills/<name>/SKILL.md`` (the agentskills.io layout)
  WARNING   - top-level fields that look Hermes-specific should live under
              ``metadata.hermes`` instead of inventing a parallel format
              (advisory only — the spec tolerates extra fields)

Exit codes: 0 = pass (warnings allowed), 1 = spec violation.

Pure functions + explicit IO so it is import-safe and unit-testable.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import yaml
except ImportError:  # pragma: no cover — CI installs pyyaml; unit tests too
    yaml = None  # type: ignore[assignment]

SKILLS_DIR_NAME = "skills"
SKILL_FILE = "SKILL.md"
MAX_NAME_LEN = 64
MAX_DESC_LEN = 1024

# Top-level fields that are NOT part of the agentskills.io spec. Their presence
# is a portability smell: an external consumer (Claude Code / Codex) will
# ignore them, and they suggest Hermes-specific data that belongs in the
# documented extension namespace (metadata.hermes). Advisory only.
_SPEC_TOP_LEVEL_KEYS = {
    "name",
    "description",
    "version",
    "license",
    "platforms",
    "compatibility",
    "inputs",
    "outputs",
    "examples",
    "metadata",
    "prerequisites",
}

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


@dataclass
class SkillViolation:
    """One spec violation for a skill manifest."""

    skill: str
    path: str
    message: str


@dataclass
class SkillReport:
    """All violations (hard) and warnings (advisory) across a skills tree.

    Hard violations break the spec's required contract (name/description/
    folder-name). Warnings flag portability smells — non-spec top-level
    fields — but the spec does not forbid extra fields, so they never fail CI.
    """

    violations: List[SkillViolation] = field(default_factory=list)
    warnings: List[SkillViolation] = field(default_factory=list)

    def add_violation(self, skill: str, path: str, message: str) -> None:
        self.violations.append(SkillViolation(skill, path, message))

    def add_warning(self, skill: str, path: str, message: str) -> None:
        self.warnings.append(SkillViolation(skill, path, message))

    @property
    def ok(self) -> bool:
        return not self.violations


def extract_frontmatter(text: str) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """Return (raw_frontmatter, parsed_dict) from SKILL.md text.

    ``(None, None)`` when the file has no ``---`` frontmatter block at all.
    """
    m = _FRONTMATTER_RE.match(text or "")
    if not m:
        return None, None
    raw = m.group(1)
    if yaml is None:
        return raw, {}
    try:
        parsed = yaml.safe_load(raw)
    except Exception:
        return raw, None  # unparseable YAML — caller reports it
    if not isinstance(parsed, dict):
        return raw, {}
    return raw, parsed


def check_skill_file(
    skill_path: Path, repo_root: Path
) -> Tuple[List[SkillViolation], List[SkillViolation]]:
    """Validate one ``skills/<name>/SKILL.md`` file.

    Returns ``(violations, warnings)``: hard spec violations (required
    name/description, folder-name match) vs advisory portability warnings
    (non-spec top-level fields — allowed by the spec, but they belong in the
    documented ``metadata`` extension namespace for portability).
    """
    skill_name = skill_path.parent.name
    out: List[SkillViolation] = []
    warnings: List[SkillViolation] = []
    rel = str(skill_path.relative_to(repo_root))

    text = skill_path.read_text(encoding="utf-8")
    raw, meta = extract_frontmatter(text)

    if raw is None:
        out.append(
            SkillViolation(
                skill_name,
                rel,
                "no YAML frontmatter (--- block) — the Agent Skills spec requires "
                "name + description frontmatter",
            )
        )
        return out, warnings

    if meta is None:
        out.append(
            SkillViolation(skill_name, rel, "frontmatter is present but not valid YAML")
        )
        return out, warnings

    name = meta.get("name")
    if not isinstance(name, str) or not name.strip():
        out.append(
            SkillViolation(skill_name, rel, "missing required 'name' in frontmatter")
        )
    else:
        if len(name) > MAX_NAME_LEN:
            out.append(
                SkillViolation(
                    skill_name,
                    rel,
                    f"name '{name}' is {len(name)} chars (max {MAX_NAME_LEN})",
                )
            )
        if name != skill_name:
            out.append(
                SkillViolation(
                    skill_name,
                    rel,
                    f"frontmatter name '{name}' does not match folder name "
                    f"'{skill_name}' — the spec ties the name to the folder",
                )
            )

    description = meta.get("description")
    if not isinstance(description, str) or not description.strip():
        out.append(
            SkillViolation(
                skill_name, rel, "missing required 'description' in frontmatter"
            )
        )
    elif len(description) > MAX_DESC_LEN:
        out.append(
            SkillViolation(
                skill_name,
                rel,
                f"description is {len(description)} chars (max {MAX_DESC_LEN})",
            )
        )

    # Extension-namespace advisory: Hermes-specific top-level keys belong under
    # metadata.hermes (documented extension). The spec tolerates extra fields,
    # so this is a WARNING, never a CI failure.
    for key in meta:
        if key not in _SPEC_TOP_LEVEL_KEYS:
            warnings.append(
                SkillViolation(
                    skill_name,
                    rel,
                    f"non-spec top-level field '{key}' — prefer the documented "
                    "extension namespace 'metadata.hermes' for portability",
                )
            )

    return out, warnings


def scan_skills_tree(skills_dir: Path) -> SkillReport:
    """Validate every ``skills/**/SKILL.md`` under a tree. Returns a report."""
    report = SkillReport()
    if not skills_dir.is_dir():
        return report
    for skill_path in sorted(skills_dir.rglob(SKILL_FILE)):
        violations, warnings = check_skill_file(skill_path, skills_dir.parent)
        for v in violations:
            report.add_violation(v.skill, v.path, v.message)
        for w in warnings:
            report.add_warning(w.skill, w.path, w.message)
    return report


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            __doc__ or "Validate skills against the Agent Skills spec"
        ).splitlines()[0]
    )
    parser.add_argument(
        "skills_dir",
        nargs="?",
        default=SKILLS_DIR_NAME,
        help="path to the skills tree (default: ./skills)",
    )
    args = parser.parse_args(argv)

    report = scan_skills_tree(Path(args.skills_dir))
    for w in report.warnings:
        print(f"SKILL_SPEC_WARNING: {w.path}: {w.message}")
    if not report.ok:
        for v in report.violations:
            print(f"SPEC_VIOLATION: {v.path}: {v.message}")
        print(f"{len(report.violations)} skill spec violation(s) found")
        return 1
    if report.warnings:
        print(f"{len(report.warnings)} skill spec warning(s) — advisory only")
    else:
        print("skills spec check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
