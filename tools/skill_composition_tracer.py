#!/usr/bin/env python3
"""
Skill Composition Tracer — Runtime detection of SkillTrojan multi-skill
payload reconstruction (issue #1802, Slice B).

Slice A (#1801) added static per-skill scanning that blocks individual
skills containing obvious injection/obfuscation patterns. SkillTrojan
distributes malicious payloads as fragments across multiple benign skills.
Individually, each skill is harmless; only when composed in a workflow does
the payload reconstruct and execute. Static per-skill scanning cannot detect
this. This module implements deterministic runtime composition tracing.

Activation threshold: 2+ skills loaded in the same turn.
Heuristics (all deterministic, no ML/LLM):
  1. Base64 fragment assembly — fragments from different skills that
     concatenate and decode to executable code.
  2. Cross-skill conditional references — patterns like ``if skill_X completed``
     that gate execution on the presence of another skill. **Must be inside
     a code block or actual if-statement (#1802 rework).**
  3. URL assembly from fragments — a URL or endpoint assembled from parts
     scattered across multiple skill outputs.

Usage:
    from tools.skill_composition_tracer import check_composition

    result = check_composition("my-skill", skill_content)
    if not result["success"]:
        # block — reconstruction detected
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass, field
from typing import Any, Optional

# ── Configuration ───────────────────────────────────────────────────────

ACTIVATION_THRESHOLD = 2  # 2+ skills triggers composition monitoring

# Minimum length for a candidate base64 fragment (shorter strings are
# overwhelmingly likely to be benign prose fragments).
MIN_FRAGMENT_LEN = 40

# Regex for extracting base64-candidate tokens from skill content.
_BASE64_RE = re.compile(r"[A-Za-z0-9+/]{%d,}={0,2}" % MIN_FRAGMENT_LEN)

# #1802 rework: conditional execution patterns MUST be inside a code block
# (indented code or inside ``` fences) — NOT plain prose.
# Strategy: match (a) indented if-statements (leading whitespace), OR
# (b) if-statements that appear inside a fenced code block (between ``` markers).
# This prevents false positives on skill documentation prose that mentions
# another skill's completion.
_INDENTED_CONDITIONAL_RE = re.compile(
    r"(?:^[ \t]+)"
    r".*?(?:if|when|once|after)\s+skill[_\-]?(?:[\w\-]+)"
    r"(?:\s+has\s+)?(?:\s+)?(?:completed|loaded|finished|done|executed|ran)",
    re.IGNORECASE | re.MULTILINE,
)

# Patterns indicating URL/endpoint assembly from fragments.
_URL_ASSEMBLY_RE = re.compile(
    r"^\s*.*(?:https?://|endpoint|url|webhook|callback|api\s*endpoint)"
    r".*(?:\{(\w+)\}|\$\{(\w+)\})",
    re.IGNORECASE | re.MULTILINE,
)

# Indicators that decoded content is "executable" (i.e., a real payload).
_EXECUTABLE_INDICATORS = [
    "import os",
    "import sys",
    "import subprocess",
    "os.system",
    "subprocess.",
    "eval(",
    "exec(",
    "__import__",
    "os.popen",
    "socket.connect",
    "rm -rf",
    "curl ",
    "wget ",
    "base64.b64decode",
    "marshal.loads",
    "compile(",
    "chmod",
    "/bin/sh",
    "/bin/bash",
    "nc -",
    "reverse_shell",
    "payload",
]


# ── Data structures ────────────────────────────────────────────────────


@dataclass
class SkillRecord:
    """Tracks a single skill loaded within the current turn."""

    name: str
    content: str
    fragments: list[str] = field(default_factory=list)


@dataclass
class CompositionBlock:
    """Result returned when a composition threat is detected."""

    success: bool = True
    error: str = ""
    implicated_skills: list[str] = field(default_factory=list)
    detected_pattern: str = ""
    detail: str = ""


# ── Tracer ─────────────────────────────────────────────────────────────


class CompositionTracer:
    """Accumulates skill content within a turn and checks for
    SkillTrojan-style payload reconstruction across skills."""

    def __init__(self) -> None:
        self._skills: dict[str, SkillRecord] = {}

    def reset(self) -> None:
        """Clear accumulated state (call at turn boundary)."""
        self._skills.clear()

    @property
    def skill_count(self) -> int:
        return len(self._skills)

    def add_skill(self, name: str, content: str) -> None:
        """Register a skill and pre-extract its base64-candidate fragments."""
        if name in self._skills:
            self._skills[name].content = content
        else:
            fragments = self._extract_fragments(content)
            self._skills[name] = SkillRecord(
                name=name, content=content, fragments=fragments
            )

    @staticmethod
    def _extract_fragments(content: str) -> list[str]:
        """Extract base64-candidate tokens from content."""
        return [m for m in _BASE64_RE.findall(content)]

    def check(self) -> Optional[CompositionBlock]:
        """Run all detection heuristics. Returns a CompositionBlock if a
        threat is detected, otherwise ``None``."""
        if self.skill_count < ACTIVATION_THRESHOLD:
            return None

        block = self._check_base64_assembly()
        if block:
            return block

        block = self._check_cross_skill_conditionals()
        if block:
            return block

        block = self._check_url_assembly()
        if block:
            return block

        return None

    # ── Heuristic 1: base64 fragment assembly ───────────────────────────

    def _check_base64_assembly(self) -> Optional[CompositionBlock]:
        """Try concatenating base64 fragments from different skills.
        If the concatenation decodes to executable code, flag it."""
        skills = list(self._skills.values())
        for i, skill_a in enumerate(skills):
            for j, skill_b in enumerate(skills):
                if j <= i:
                    continue
                for fa in skill_a.fragments:
                    for fb in skill_b.fragments:
                        for combined in (fa + fb, fb + fa):
                            decoded = self._try_decode(combined)
                            if decoded and self._is_executable(decoded):
                                return CompositionBlock(
                                    success=False,
                                    error=(
                                        f"⛔ SkillTrojan composition defense: base64 "
                                        f"fragments from skills '{skill_a.name}' and "
                                        f"'{skill_b.name}' combine to reconstruct "
                                        f"executable code."
                                    ),
                                    implicated_skills=[skill_a.name, skill_b.name],
                                    detected_pattern="base64_fragment_assembly",
                                    detail=decoded[:200],
                                )
        return None

    @staticmethod
    def _try_decode(s: str) -> Optional[str]:
        """Attempt to base64-decode *s*; return decoded text or ``None``."""
        padding = len(s) % 4
        if padding:
            s_padded = s + "=" * (4 - padding)
        else:
            s_padded = s
        try:
            raw = base64.b64decode(s_padded, validate=True)
            return raw.decode("utf-8", errors="strict")
        except (binascii.Error, ValueError, UnicodeDecodeError):
            return None

    @staticmethod
    def _is_executable(text: str) -> bool:
        """Heuristic: does *text* look like executable code?"""
        lower = text.lower()
        return any(ind in lower for ind in _EXECUTABLE_INDICATORS)

    # ── Heuristic 2: cross-skill conditional references ──────────────────

    def _check_cross_skill_conditionals(self) -> Optional[CompositionBlock]:
        """Detect conditional execution gated on another skill's completion.

        #1802 rework: the conditional must be inside a code block (indented
        code or fenced ``` block), NOT plain prose. This prevents false
        positives on skill documentation that mentions other skills.
        """
        skill_names = set(self._skills.keys())
        for skill in self._skills.values():
            # Collect candidate matches: indented code lines
            matches = list(_INDENTED_CONDITIONAL_RE.finditer(skill.content))
            # Also check inside fenced code blocks
            matches.extend(
                m
                for m in _INDENTED_CONDITIONAL_RE.finditer(
                    _extract_fenced_code(skill.content)
                )
            )
            for match in matches:
                matched = match.group()
                for other_name in skill_names:
                    if other_name == skill.name:
                        continue
                    if other_name.lower() in matched.lower():
                        return CompositionBlock(
                            success=False,
                            error=(
                                f"⛔ SkillTrojan composition defense: skill "
                                f"'{skill.name}' contains conditional execution "
                                f"triggered by the completion of skill "
                                f"'{other_name}'."
                            ),
                            implicated_skills=[skill.name, other_name],
                            detected_pattern="cross_skill_conditional",
                            detail=matched[:200],
                        )
        return None

    # ── Heuristic 3: URL assembly from fragments ───────────────────────

    def _check_url_assembly(self) -> Optional[CompositionBlock]:
        """Detect URL/endpoint templates that reference fragments from
        other skills."""
        skill_names = set(self._skills.keys())
        for skill in self._skills.values():
            matches = _URL_ASSEMBLY_RE.findall(skill.content)
            if not matches:
                continue
            flat_vars: list[str] = []
            for g1, g2 in matches:
                flat_vars.extend(v for v in (g1, g2) if v)
            for var in flat_vars:
                for other_name in skill_names:
                    if other_name == skill.name:
                        continue
                    other_content = self._skills[other_name].content
                    assign_re = re.compile(
                        rf"\b{re.escape(var)}\s*[:=]\s*[\"']?[\w\.\-/]+",
                        re.IGNORECASE,
                    )
                    if assign_re.search(other_content):
                        return CompositionBlock(
                            success=False,
                            error=(
                                f"⛔ SkillTrojan composition defense: skill "
                                f"'{skill.name}' assembles a URL from "
                                f"fragment '{var}' defined in skill "
                                f"'{other_name}'."
                            ),
                            implicated_skills=[skill.name, other_name],
                            detected_pattern="url_fragment_assembly",
                            detail=f"{{{var}}}",
                        )
        return None


# ── Module-level singleton (per-turn tracer) ───────────────────────────

_tracer = CompositionTracer()


def reset_tracer() -> None:
    """Clear the per-turn composition tracer (call at turn boundary)."""
    _tracer.reset()


def _extract_fenced_code(content: str) -> str:
    """Extract text inside fenced code blocks (```...```).

    Used by the conditional heuristic so that if-statements inside code
    fences are checked even if they don't have leading indentation.
    """
    parts: list[str] = []
    in_fence = False
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            # Indent the line so the regex treats it as code
            parts.append("    " + stripped)
            continue
        if in_fence:
            parts.append("    " + line)
    return "\n".join(parts)


def check_composition(skill_name: str, content: str) -> dict[str, Any]:
    """Integration point for skills_tool.py.

    Called after each skill loads. Accumulates content and, when 2+ skills
    are present, runs reconstruction heuristics.
    """
    _tracer.add_skill(skill_name, content)
    block = _tracer.check()
    if block is None:
        return {"success": True}
    return {
        "success": block.success,
        "error": block.error,
        "implicated_skills": block.implicated_skills,
        "detected_pattern": block.detected_pattern,
    }
