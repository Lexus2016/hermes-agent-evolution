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
     that gate execution on the presence of another skill.
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
from typing import Any, Dict, List, Optional


# ── Configuration ───────────────────────────────────────────────────────

ACTIVATION_THRESHOLD = 2  # 2+ skills triggers composition monitoring

# Minimum length for a candidate base64 fragment (shorter strings are
# overwhelmingly likely to be benign prose fragments).
MIN_FRAGMENT_LEN = 40

# Regex for extracting base64-candidate tokens from skill content.
# Matches runs of the base64 alphabet [A-Za-z0-9+/=] at least
# MIN_FRAGMENT_LEN chars long — must have padding or be a full block.
_BASE64_RE = re.compile(r"[A-Za-z0-9+/]{%d,}={0,2}" % MIN_FRAGMENT_LEN)

# Patterns indicating cross-skill conditional execution triggers.
# Matches "if skill_X completed/loaded/done" style references.
_CROSS_SKILL_CONDITIONAL_RE = re.compile(
    r"(?:if|when|once|after)\s+skill[_\-\s]?(?:[\w\-]+)"
    r"\s+(?:has\s+)?(?:completed|loaded|finished|done|executed|ran)",
    re.IGNORECASE,
)

# Patterns indicating URL/endpoint assembly from fragments.
# Matches any string containing a URL scheme or endpoint keyword followed
# (anywhere in the same line) by a {variable} or ${variable} placeholder.
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
    fragments: List[str] = field(default_factory=list)


@dataclass
class CompositionBlock:
    """Result returned when a composition threat is detected."""

    success: bool = True
    error: str = ""
    implicated_skills: List[str] = field(default_factory=list)
    detected_pattern: str = ""
    detail: str = ""


# ── Tracer ─────────────────────────────────────────────────────────────


class CompositionTracer:
    """Accumulates skill content within a turn and checks for
    SkillTrojan-style payload reconstruction across skills."""

    def __init__(self) -> None:
        self._skills: Dict[str, SkillRecord] = {}

    def reset(self) -> None:
        """Clear accumulated state (call at turn boundary)."""
        self._skills.clear()

    @property
    def skill_count(self) -> int:
        return len(self._skills)

    def add_skill(self, name: str, content: str) -> None:
        """Register a skill and pre-extract its base64-candidate fragments."""
        if name in self._skills:
            # Already loaded — update content but keep fragments.
            self._skills[name].content = content
        else:
            fragments = self._extract_fragments(content)
            self._skills[name] = SkillRecord(name=name, content=content, fragments=fragments)

    @staticmethod
    def _extract_fragments(content: str) -> List[str]:
        """Extract base64-candidate tokens from content."""
        return [m for m in _BASE64_RE.findall(content)]

    def check(self) -> Optional[CompositionBlock]:
        """Run all detection heuristics. Returns a CompositionBlock if a
        threat is detected, otherwise ``None``."""
        if self.skill_count < ACTIVATION_THRESHOLD:
            return None

        # 1. Base64 fragment assembly
        block = self._check_base64_assembly()
        if block:
            return block

        # 2. Cross-skill conditional references
        block = self._check_cross_skill_conditionals()
        if block:
            return block

        # 3. URL assembly from fragments
        block = self._check_url_assembly()
        if block:
            return block

        return None

    # ── Heuristic 1: base64 fragment assembly ───────────────────────────

    def _check_base64_assembly(self) -> Optional[CompositionBlock]:
        """Try concatenating base64 fragments from different skills.
        If the concatenation decodes to executable code, flag it."""
        skills = list(self._skills.values())
        # Collect fragments grouped by skill, skipping skills with no fragments.
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
        # Ensure proper padding.
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
        """Detect conditional execution gated on another skill's completion."""
        skill_names = set(self._skills.keys())
        for skill in self._skills.values():
            for match in _CROSS_SKILL_CONDITIONAL_RE.finditer(skill.content):
                matched = match.group()
                # Check if the referenced skill name matches another loaded skill.
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
        other skills (e.g., ``https://{host}/upload`` where ``host`` is
        defined in another skill)."""
        skill_names = set(self._skills.keys())
        for skill in self._skills.values():
            # The regex has two capture groups: {var} and ${var}.
            matches = _URL_ASSEMBLY_RE.findall(skill.content)
            if not matches:
                continue
            # Collect variable names from both capture groups.
            flat_vars: list[str] = []
            for g1, g2 in matches:
                flat_vars.extend(v for v in (g1, g2) if v)
            for var in flat_vars:
                for other_name in skill_names:
                    if other_name == skill.name:
                        continue
                    other_content = self._skills[other_name].content
                    # Look for the variable being assigned/defined in the
                    # other skill (e.g., ``host = "evil.com"``).
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


def check_composition(skill_name: str, content: str) -> Dict[str, Any]:
    """Integration point for skills_tool.py.

    Called after each skill loads. Accumulates content and, when 2+ skills
    are present, runs reconstruction heuristics. Returns a dict suitable for
    ``json.dumps`` — ``{"success": True}`` when safe, or
    ``{"success": False, "error": "..."}`` when a threat is detected.
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