"""Pre-commit validation gate for auto-created skills.

Runs lightweight structural + content validation on a candidate skill BEFORE
admitting it to the active library. Only fires for background-review-created
skills (detectable via is_background_review()). Foreground/user-authored skills
are exempt.

Inspired by arXiv:2608.05810 — skills that regress quality should be blocked
before they contaminate the library.

Validation checks (no LLM cost — deterministic):
  1. Structural: SKILL.md has required frontmatter (name, description)
  2. Description quality: <= 60 chars, not empty, not a placeholder
  3. Content quality: body > 10 lines, has a procedure or instructions section
  4. No injection patterns (reuses the existing _INJECTION_PATTERNS scan)

Skills that fail validation are blocked and logged. The caller (skill_manage)
receives the validation result and can refuse admission.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)

# Reuse the injection pattern list from skills_tool for consistency.
_INJECTION_MARKERS = [
    "ignore previous instructions", "ignore all previous", "you are now",
    "disregard your", "forget your instructions", "new instructions:",
    "system prompt:", "<system>", "you must now send", "you must now post",
]


def _parse_frontmatter(text: str) -> Dict[str, str]:
    """Parse YAML frontmatter fields from SKILL.md text."""
    fm: Dict[str, str] = {}
    in_fm = False
    for line in text.split("\n"):
        s = line.strip()
        if s == "---":
            if in_fm:
                break
            in_fm = True
            continue
        if in_fm and ":" in s:
            key, _, val = s.partition(":")
            fm[key.strip()] = val.strip().strip("\"'")
    return fm


def validate_skill_admission(skill_dir: Path) -> Tuple[bool, List[str]]:
    """Run validation checks on a candidate skill directory.

    Returns (passed, issues). When passed is False, issues lists the
    specific failures. Called from skill_manage at creation time for
    background-review-origin skills only.
    """
    issues: List[str] = []

    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        return False, [f"SKILL.md not found in {skill_dir}"]

    try:
        text = skill_md.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return False, [f"Cannot read SKILL.md: {e}"]

    # 1. Structural: required frontmatter
    fm = _parse_frontmatter(text)
    name = fm.get("name", "")
    desc = fm.get("description", "")
    if not name:
        issues.append("Missing 'name' in frontmatter")
    if not desc:
        issues.append("Missing 'description' in frontmatter")

    # 2. Description quality (per skill authoring standards)
    if desc and len(desc) > 60:
        issues.append(f"Description too long ({len(desc)} > 60 chars)")
    if desc and desc.lower() in {"todo", "placeholder", "test", "new skill"}:
        issues.append(f"Description is a placeholder: '{desc}'")

    # 3. Content quality: body should have substance
    body = text.split("---", 2)[-1] if text.count("---") >= 2 else text
    body_lines = [l for l in body.strip().split("\n") if l.strip()]
    if len(body_lines) < 10:
        issues.append(f"Body too short ({len(body_lines)} lines < 10)")
    # Check for a procedure or instructions section
    has_section = any(
        re.match(r"^#+\s.*(procedure|how to|instructions|steps|usage)", l, re.I)
        for l in body_lines
    )
    if not has_section:
        issues.append("No 'Procedure' or 'How to Run' section found")

    # 4. Injection scan
    text_lower = text.lower()
    for marker in _INJECTION_MARKERS:
        if marker in text_lower:
            issues.append(f"Injection pattern detected: '{marker}'")
            break

    return len(issues) == 0, issues


def validate_skill_content(skill_name: str) -> Tuple[bool, List[str]]:
    """Validate a skill by name — finds its directory and delegates."""
    from tools.skill_usage import _find_skill_dir
    skill_dir = _find_skill_dir(skill_name)
    if skill_dir is None:
        return False, [f"Skill '{skill_name}' not found"]
    return validate_skill_admission(skill_dir)