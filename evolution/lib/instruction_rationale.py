"""Instruction provenance: structured rationale blocks for skills/instructions (#2629).

Catastrophic-remembering guard (arXiv:2608.11095): an instruction that does not
carry its "why" — the failure that triggered it, the hypothesis, and the observed
outcome — accumulates as noise and can never be safely pruned. This module parses
the YAML frontmatter of instruction files (SKILL.md / AGENTS.md style) and
validates a ``rationale`` block of exactly that shape:

    failure:   what broke that this instruction exists to prevent
    hypothesis: what change was believed to fix it
    outcome:   observed result after the change (empty while unverified)

An instruction is "decayed" when its rationale is missing, malformed, or no
longer referenced by the body. The evolution loop can use :func:`scan_skills_dir`
as a gate to flag decayed instructions before they become unreferenced weight.
stdlib + PyYAML only; no agent-core imports.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

REQUIRED_RATIONALE_KEYS = ("failure", "hypothesis", "outcome")


@dataclass
class RationaleReport:
    """Validation result for one instruction file."""

    path: str
    name: str = ""
    ok: bool = True
    problems: List[str] = field(default_factory=list)


def extract_frontmatter(text: str) -> Optional[Dict[str, Any]]:
    """Return the YAML frontmatter dict of a markdown file, or None if absent."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return None
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return None
    try:
        import yaml

        parsed = yaml.safe_load("\n".join(lines[1:end]))
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def validate_rationale(frontmatter: Optional[Dict[str, Any]]) -> List[str]:
    """Validate the ``rationale`` block of parsed frontmatter; return problems."""
    problems: List[str] = []
    if not isinstance(frontmatter, dict):
        return ["missing YAML frontmatter"]
    rationale = frontmatter.get("rationale")
    if not isinstance(rationale, dict):
        return ["missing 'rationale' block (failure/hypothesis/outcome)"]
    for key in REQUIRED_RATIONALE_KEYS:
        if key not in rationale or not str(rationale.get(key) or "").strip():
            problems.append(f"rationale missing non-empty '{key}'")
    return problems


def rationale_referenced(text: str, frontmatter: Optional[Dict[str, Any]]) -> bool:
    """False when the rationale's failure phrase no longer appears in the body."""
    if not isinstance(frontmatter, dict) or not isinstance(
        frontmatter.get("rationale"), dict
    ):
        return True
    failure = str(frontmatter["rationale"].get("failure") or "").strip()
    if not failure:
        return True
    body = text.split("---", 2)[-1] if text.count("---") >= 2 else text
    probe = failure.split(" ")[0].strip("`.,:;()[]")
    return len(probe) >= 3 and probe in body


def check_instruction_file(path: Path) -> RationaleReport:
    """Validate one instruction file (SKILL.md / AGENTS.md style)."""
    report = RationaleReport(path=str(path))
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        report.ok = False
        report.problems.append(f"unreadable: {exc}")
        return report
    frontmatter = extract_frontmatter(text)
    if frontmatter is None:
        report.ok = False
        report.problems.append("no YAML frontmatter (instructions without provenance)")
        return report
    report.name = str(frontmatter.get("name", ""))
    report.problems = validate_rationale(frontmatter)
    if not rationale_referenced(text, frontmatter):
        report.problems.append(
            "rationale failure phrase not referenced in body (decayed)"
        )
    report.ok = not report.problems
    return report


def scan_skills_dir(skills_dir: Path) -> List[RationaleReport]:
    """Check every SKILL.md under *skills_dir*; return one report per file."""
    reports: List[RationaleReport] = []
    if not skills_dir.exists():
        return reports
    for skill_md in sorted(skills_dir.rglob("SKILL.md")):
        reports.append(check_instruction_file(skill_md))
    return reports
