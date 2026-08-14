"""Skill contract extraction — read-only Slice A of SkillZip (#2414, parent #2382).

``extract_contract`` parses a SKILL.md into a typed structural contract
(interface, workflow, tool protocol, scoped rules). Read-only; defensive — malformed input yields empty fields, never an exception.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List

__all__ = ["SkillContract", "extract_contract"]

_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*(?:\n|\Z)", re.DOTALL)
_SECTION_RE = re.compile(
    r"^##+[ \t]*(?P<title>[^\n]+)\n(?P<body>.*?)(?=^##+[ \t]*|\Z)",
    re.DOTALL | re.MULTILINE,
)
_LIST_ITEM_RE = re.compile(
    r"^[ \t]*(?:\d+\.|[-*])[ \t]+(?P<text>.+?)[ \t]*$", re.MULTILINE
)
_TOOL_TOKEN_RE = re.compile(r"`([a-z][a-z0-9_]{1,63})`")
_SKILL_NAME_RE = re.compile(r"\A[a-z0-9][a-z0-9_-]{0,63}\Z")
_WORKFLOW_TITLE_RE = re.compile(
    r"workflow|procedure|process|phases|steps", re.IGNORECASE
)
_RULE_TITLE_RE = re.compile(
    r"when to use|pitfall|checklist|iron law|red flag|constraint", re.IGNORECASE
)


def _parse_frontmatter(text: str) -> Dict[str, str]:
    """Flat scalar frontmatter keys (nested blocks ignored). Never raises."""
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        return {}
    try:  # PyYAML is always importable in-repo; degrade to {} on any error.
        import yaml  # type: ignore

        data = yaml.safe_load(match.group(1))
        if isinstance(data, dict):
            return {
                str(k): v
                for k, v in data.items()
                if isinstance(v, (str, int, float)) and not isinstance(v, bool)
            }
    except Exception:
        pass
    return {}


def _strip_markup(text: str) -> str:
    """Drop checkbox markers, bold, and backticks from one list item."""
    cleaned = re.sub(r"^\[[ xX]\][ \t]*", "", text.strip())
    return cleaned.replace("**", "").replace("`", "").strip()


def _dedupe(items: List[str]) -> List[str]:
    """Order-preserving dedupe."""
    seen: set = set()
    return [i for i in items if not (i in seen or seen.add(i))]


@dataclass
class SkillContract:
    """Typed structural contract extracted from one SKILL.md."""

    skill_name: str = ""
    description: str = ""
    version: str = ""
    workflow: List[str] = field(default_factory=list)
    tools: List[str] = field(default_factory=list)
    scoped_rules: List[str] = field(default_factory=list)
    source_chars: int = 0


def extract_contract(skill_markdown: str) -> SkillContract:
    """Parse one SKILL.md (read-only) into its structural contract.

    Never raises: missing frontmatter or absent sections produce empty
    fields. ``skill_name`` keeps the lowercase-hyphen convention — values
    that violate it degrade to ``""``.
    """
    text = skill_markdown if isinstance(skill_markdown, str) else ""
    front = _parse_frontmatter(text)
    workflow: List[str] = []
    scoped: List[str] = []
    for match in _SECTION_RE.finditer(text):
        title = match.group("title").strip().lower()
        items = [
            _strip_markup(m.group("text"))
            for m in _LIST_ITEM_RE.finditer(match.group("body"))
        ]
        if _WORKFLOW_TITLE_RE.search(title):
            workflow.extend(items)
        elif _RULE_TITLE_RE.search(title):
            scoped.extend(items)
    raw_name = str(front.get("name", "") or "")
    return SkillContract(
        skill_name=raw_name if _SKILL_NAME_RE.match(raw_name) else "",
        description=str(front.get("description", "") or ""),
        version=str(front.get("version", "") or ""),
        workflow=workflow,
        tools=_dedupe(_TOOL_TOKEN_RE.findall(text)),
        scoped_rules=_dedupe(scoped),
        source_chars=len(text),
    )
