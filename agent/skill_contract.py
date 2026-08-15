"""Typed structural contract extraction for Hermes skills (#2414 / parent #2382).

Provides read-only extraction of structural contracts (interface, workflow steps,
tool protocol, scoped rules) from skill markdown without rewriting or mutation.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class SkillInterface:
    name: str = ""
    description: str = ""
    version: str = "1.0.0"
    platforms: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)


@dataclass
class SkillWorkflowStep:
    index: int
    title: str
    description: str = ""
    code_snippets: list[str] = field(default_factory=list)


@dataclass
class SkillToolProtocol:
    required_tools: list[str] = field(default_factory=list)
    environment_variables: list[str] = field(default_factory=list)


@dataclass
class SkillScopedRule:
    category: str
    rule: str


@dataclass
class SkillContract:
    name: str
    interface: SkillInterface
    workflow: list[SkillWorkflowStep] = field(default_factory=list)
    tools: SkillToolProtocol = field(default_factory=SkillToolProtocol)
    rules: list[SkillScopedRule] = field(default_factory=list)
    raw_char_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)


_KNOWN_TOOLS = frozenset({
    "gh",
    "git",
    "python",
    "pytest",
    "curl",
    "jq",
    "sed",
    "awk",
    "grep",
    "uv",
    "node",
    "npm",
    "cargo",
    "docker",
    "read_file",
    "write_file",
    "patch",
    "terminal",
    "search_files",
    "execute_code",
    "vision_analyze",
})


def extract_skill_contract(skill_markdown: str) -> SkillContract:
    """Extract a typed structural contract from raw skill markdown."""
    import yaml

    content = skill_markdown.strip()
    raw_len = len(content)

    interface = SkillInterface()
    body = content

    # 1. Parse YAML frontmatter
    fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
    if fm_match:
        try:
            fm_data = yaml.safe_load(fm_match.group(1)) or {}
            interface.name = str(fm_data.get("name", ""))
            interface.description = str(fm_data.get("description", ""))
            interface.version = str(fm_data.get("version", "1.0.0"))
            platforms = fm_data.get("platforms", [])
            interface.platforms = (
                platforms if isinstance(platforms, list) else [str(platforms)]
            )
            meta = fm_data.get("metadata", {})
            if (
                isinstance(meta, dict)
                and "hermes" in meta
                and isinstance(meta["hermes"], dict)
            ):
                interface.tags = meta["hermes"].get("tags", [])
        except Exception:
            pass
        body = content[fm_match.end() :]

    # 2. Extract sections by headings
    workflow: list[SkillWorkflowStep] = []
    rules: list[SkillScopedRule] = []
    detected_tools: set[str] = set()
    detected_env_vars: set[str] = set()

    # Detect code blocks & tools/env vars inside them
    code_blocks = re.findall(r"```(?:\w+)?\n(.*?)```", body, re.DOTALL)
    for block in code_blocks:
        # Detect tool commands
        for word in re.findall(r"\b[a-zA-Z_][a-zA-Z0-9_\-]*\b", block):
            if word in _KNOWN_TOOLS:
                detected_tools.add(word)
        # Detect environment variables
        for var in re.findall(
            r"\$([A-Z_][A-Z0-9_]+)|\$\{([A-Z_][A-Z0-9_]+)\}|([A-Z_][A-Z0-9_]+)=", block
        ):
            env_name = next(v for v in var if v)
            if len(env_name) > 2:
                detected_env_vars.add(env_name)

    # Parse headings and classify as rules vs workflow steps
    sections = re.split(r"\n(?=#{1,3}\s+)", body)
    step_idx = 1
    for sec in sections:
        sec = sec.strip()
        if not sec:
            continue
        lines = sec.split("\n")
        header = lines[0].lstrip("#").strip()
        sec_body = "\n".join(lines[1:]).strip()
        sec_lower = header.lower()

        if any(
            cat in sec_lower
            for cat in ("prerequisite", "constraint", "rule", "security", "guard")
        ):
            category = (
                "prerequisite"
                if "prereq" in sec_lower
                else ("constraint" if "constraint" in sec_lower else "rule")
            )
            bullet_items = re.findall(r"^[*-]\s+(.+)$", sec_body, re.MULTILINE)
            if bullet_items:
                for item in bullet_items:
                    rules.append(SkillScopedRule(category=category, rule=item.strip()))
            elif sec_body:
                rules.append(SkillScopedRule(category=category, rule=sec_body))
        else:
            snippets = re.findall(r"```(?:\w+)?\n(.*?)```", sec, re.DOTALL)
            desc = re.sub(r"```(?:\w+)?\n.*?```", "", sec_body, flags=re.DOTALL).strip()
            workflow.append(
                SkillWorkflowStep(
                    index=step_idx,
                    title=header,
                    description=desc,
                    code_snippets=snippets,
                )
            )
            step_idx += 1

    return SkillContract(
        name=interface.name or "unnamed_skill",
        interface=interface,
        workflow=workflow,
        tools=SkillToolProtocol(
            required_tools=sorted(detected_tools),
            environment_variables=sorted(detected_env_vars),
        ),
        rules=rules,
        raw_char_count=raw_len,
    )
