# -*- coding: utf-8 -*-
"""Skill contract extraction — read-only Slice A of SkillZip (#2382, #2414).

``extract_contract`` parses an existing SKILL.md and returns a typed
structural contract — interface, workflow, tool protocol, scoped rules —
as a standalone JSON-serializable object. Read-only by design: no
compression or rewriting happens here (that is Slice B, #2415, which
gates on this contract existing first).

Style mirrors ``agent/experience_bank.py``: pure functions + dataclasses,
standard library only, defensive everywhere — a malformed skill yields a
contract with empty fields, never an exception. PyYAML is used when
importable (it always is in-repo); a tolerant line parser is the
fallback so the module never hard-depends on it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List

__version__ = "1.0.0"
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
    """Return flat scalar frontmatter keys (``name``, ``description``, ...).

    Nested blocks (``metadata:``) are ignored by both paths so the return
    type stays ``Dict[str, str]``. Never raises on malformed input.
    """
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        return {}
    raw = match.group(1)
    try:  # Prefer real YAML when importable.
        import yaml  # type: ignore

        data = yaml.safe_load(raw)
        if isinstance(data, dict):
            return {
                str(k): v
                for k, v in data.items()
                if isinstance(v, (str, int, float)) and not isinstance(v, bool)
            }
    except Exception:
        pass  # Fall through to the tolerant line parser below.
    parsed: Dict[str, str] = {}
    for line in raw.splitlines():
        if not line or line[0] in " \t#" or ":" not in line:
            continue  # Top-level scalar keys only; skip nesting/comments.
        key, _, value = line.partition(":")
        parsed[key.strip()] = value.strip().strip("'\"")
    return parsed


def _strip_markup(text: str) -> str:
    """Drop checkbox markers, bold, and backticks from one list item."""
    cleaned = re.sub(r"^\[[ xX]\][ \t]*", "", text.strip())
    return cleaned.replace("**", "").replace("`", "").strip()


def _dedupe(items: List[str]) -> List[str]:
    """Order-preserving dedupe."""
    seen: set = set()
    out: List[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


@dataclass
class SkillContract:
    """Typed structural contract extracted from one SKILL.md.

    Slice B (#2415) will use this as its coverage gate: a compressed
    skill must still satisfy its own contract.
    """

    skill_name: str = ""
    description: str = ""
    version: str = ""
    license: str = ""
    workflow: List[str] = field(default_factory=list)
    tools: List[str] = field(default_factory=list)
    scoped_rules: List[str] = field(default_factory=list)
    section_titles: List[str] = field(default_factory=list)
    source_chars: int = 0

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a JSON-compatible dict."""
        return {
            "skill_name": self.skill_name,
            "description": self.description,
            "version": self.version,
            "license": self.license,
            "workflow": list(self.workflow),
            "tools": list(self.tools),
            "scoped_rules": list(self.scoped_rules),
            "section_titles": list(self.section_titles),
            "source_chars": self.source_chars,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SkillContract":
        """Tolerant deserialization — malformed fields coerce to defaults."""

        def _s(key: str) -> str:
            return str(d.get(key, "") or "")

        def _l(key: str) -> List[str]:
            raw = d.get(key)
            return [str(x) for x in raw] if isinstance(raw, list) else []

        try:
            chars = int(d.get("source_chars", 0) or 0)
        except (TypeError, ValueError):
            chars = 0
        return cls(
            skill_name=_s("skill_name"),
            description=_s("description"),
            version=_s("version"),
            license=_s("license"),
            workflow=_l("workflow"),
            tools=_l("tools"),
            scoped_rules=_l("scoped_rules"),
            section_titles=_l("section_titles"),
            source_chars=chars,
        )


def extract_contract(skill_markdown: str) -> SkillContract:
    """Parse one SKILL.md (read-only) into its structural contract.

    Never raises: missing frontmatter or absent sections simply produce
    empty fields, so callers can extract defensively at scale. The
    ``skill_name`` keeps the repo's lowercase-hyphen convention — values
    that violate it (e.g. from corrupt frontmatter) degrade to ``""``.
    """
    text = skill_markdown if isinstance(skill_markdown, str) else ""
    front = _parse_frontmatter(text)
    sections = list(_SECTION_RE.finditer(text))
    titles = [m.group("title").strip() for m in sections]
    bodies = {m.group("title").strip().lower(): m.group("body") for m in sections}

    def _items(body: str) -> List[str]:
        return [_strip_markup(m.group("text")) for m in _LIST_ITEM_RE.finditer(body)]

    workflow: List[str] = []
    scoped: List[str] = []
    for title, body in bodies.items():
        if _WORKFLOW_TITLE_RE.search(title):
            workflow.extend(_items(body))
        elif _RULE_TITLE_RE.search(title):
            scoped.extend(_items(body))
    raw_name = str(front.get("name", "") or "")
    return SkillContract(
        skill_name=raw_name if _SKILL_NAME_RE.match(raw_name) else "",
        description=str(front.get("description", "") or ""),
        version=str(front.get("version", "") or ""),
        license=str(front.get("license", "") or ""),
        workflow=workflow,
        tools=_dedupe(_TOOL_TOKEN_RE.findall(text)),
        scoped_rules=_dedupe(scoped),
        section_titles=titles,
        source_chars=len(text),
    )
