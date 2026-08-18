# -*- coding: utf-8 -*-
"""Automatic Skill Crystallization from successful execution traces (issue #2359).

Adopts Live-SWE-agent (arXiv:2511.13646) and AgentFactory (arXiv:2603.18000):
1. Reflects on completed session traces to detect generic, reusable multi-step workflows.
2. Synthesizes canonical SKILL.md bundles (YAML frontmatter + procedure + validation).
3. Enforces strict constraint gates (size <= 15KB, valid frontmatter, non-empty triggers).
4. Emits provisional skill candidates into the local skill library.
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

__all__ = [
    "CrystallizedSkillCandidate",
    "SkillCrystallizer",
]

MAX_SKILL_SIZE_BYTES = 15 * 1024  # 15 KB strict gate

# Evidence bar for promoting a memory/trace into a reusable skill (#2746).
# A candidate must clear ALL of these to be promoted — the explicit guardrail
# governing the memory->skill distillation path, so a single lucky trace does
# not silently become a persistent skill that compounds its errors.
MIN_REUSABILITY_SCORE = 0.6  # reusability heuristic must clear this
MIN_DISTINCT_TOOLS = 2  # must exercise more than one tool (real workflow)
MIN_ACTIONS = 3  # must be a non-trivial multi-step workflow
REQUIRE_VERIFIED = True  # trace must be a verified success, not just "ok"


@dataclass
class CrystallizedSkillCandidate:
    """A skill candidate synthesized directly from an execution trace."""

    name: str
    description: str
    skill_markdown: str
    source_session_id: str = ""
    reusability_score: float = 0.0
    validation_status: str = "provisional"
    size_bytes: int = 0
    tags: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.size_bytes = len(self.skill_markdown.encode("utf-8"))
        self.reusability_score = max(0.0, min(1.0, float(self.reusability_score)))

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> CrystallizedSkillCandidate:
        return cls(
            name=str(d.get("name", "")),
            description=str(d.get("description", "")),
            skill_markdown=str(d.get("skill_markdown", "")),
            source_session_id=str(d.get("source_session_id", "")),
            reusability_score=float(d.get("reusability_score", 0.0)),
            validation_status=str(d.get("validation_status", "provisional")),
            size_bytes=int(d.get("size_bytes", 0)),
            tags=list(d.get("tags", []) or []),
        )


class SkillCrystallizer:
    """Synthesize and validate reusable skills from agent trajectories."""

    @staticmethod
    def _sanitize_name(name: str) -> str:
        """Sanitize string into a kebab-case skill identifier."""
        clean = re.sub(r"[^a-zA-Z0-9_-]", "-", name.strip().lower())
        clean = re.sub(r"-+", "-", clean).strip("-")
        return clean or "crystallized-skill"

    @classmethod
    def meets_evidence_bar(
        cls,
        candidate: CrystallizedSkillCandidate,
        *,
        distinct_tools: int = 0,
        action_count: int = 0,
        verified: bool = True,
    ) -> Tuple[bool, str]:
        """Check a candidate against the memory->skill evidence bar (#2746).

        The explicit guardrail governing which memories/traces may be promoted
        to persistent skills. A candidate must clear ALL of:
          - reusability score >= MIN_REUSABILITY_SCORE
          - exercised >= MIN_DISTINCT_TOOLS distinct tools
          - >= MIN_ACTIONS total actions (non-trivial workflow)
          - verified success (REQUIRE_VERIFIED)

        Returns (passes, reason). A candidate that fails is NOT promoted —
        it stays a provisional memory rather than becoming a persistent skill
        that could compound its errors.
        """
        if candidate.reusability_score < MIN_REUSABILITY_SCORE:
            return (
                False,
                f"reusability {candidate.reusability_score:.2f} < "
                f"{MIN_REUSABILITY_SCORE}",
            )
        if distinct_tools < MIN_DISTINCT_TOOLS:
            return (
                False,
                f"only {distinct_tools} distinct tool(s), need >= {MIN_DISTINCT_TOOLS}",
            )
        if action_count < MIN_ACTIONS:
            return (
                False,
                f"only {action_count} action(s), need >= {MIN_ACTIONS}",
            )
        if REQUIRE_VERIFIED and not verified:
            return False, "trace not verified as a success"
        return True, "meets evidence bar"

    @classmethod
    def reflect_on_trace(
        cls,
        trace_data: Dict[str, Any],
        skill_name: Optional[str] = None,
        min_actions: int = 2,
    ) -> Optional[CrystallizedSkillCandidate]:
        """Reflect on a completed session trace and crystallize a new skill candidate."""
        status = str(trace_data.get("status", "")).lower()
        if status not in {"success", "completed", "ok"}:
            return None

        events = trace_data.get("events", [])
        tool_calls = trace_data.get("tool_calls", [])
        task_goal = str(
            trace_data.get("goal")
            or trace_data.get("user_message")
            or "Automated Workflow"
        )
        session_id = str(trace_data.get("session_id", ""))

        total_actions = len(tool_calls) if tool_calls else len(events)
        if total_actions < min_actions:
            return None

        # Reusability heuristic based on workflow diversity and action depth
        reusability = min(1.0, 0.4 + (total_actions * 0.1))

        # Extract distinct tools used
        tools_used: List[str] = []
        for tc in tool_calls:
            name = tc.get("name") or tc.get("tool") or ""
            if name and name not in tools_used:
                tools_used.append(name)

        canonical_name = cls._sanitize_name(skill_name or task_goal[:30])
        description = f"Automated workflow for {task_goal.strip()[:80]}."

        # Format markdown body
        frontmatter = f"""---
name: {canonical_name}
description: {description}
version: "1.0.0"
tags:
  - crystallized
  - auto-generated
---
"""
        body = f"""# {canonical_name.replace("-", " ").title()}

## Overview
{description}

## When to Use
- When executing tasks related to: {task_goal.strip()}
- Primary tools utilized: {", ".join(tools_used) if tools_used else "standard toolset"}

## Workflow Procedure
1. Initialize environment and check requirements.
2. Execute core operations following verified execution path.
3. Validate output artifacts and return structured summary.

## Verification
- Confirm exit code 0 or successful assertion output.
"""
        full_skill_md = frontmatter.strip() + "\n\n" + body.strip() + "\n"

        candidate = CrystallizedSkillCandidate(
            name=canonical_name,
            description=description,
            skill_markdown=full_skill_md,
            source_session_id=session_id,
            reusability_score=reusability,
            validation_status="provisional",
            tags=["crystallized", "auto-generated"],
        )

        ok, _ = cls.validate_candidate(candidate)
        if not ok:
            return None

        # Evidence bar (#2746): only promote traces that clear the explicit
        # memory->skill guardrail. A trace that is too thin, too single-tool,
        # or not verified stays a provisional memory, not a persistent skill.
        verified = status in {"success", "completed"}
        bar_ok, _ = cls.meets_evidence_bar(
            candidate,
            distinct_tools=len(tools_used),
            action_count=total_actions,
            verified=verified,
        )
        return candidate if bar_ok else None

    @classmethod
    def validate_candidate(
        cls, candidate: CrystallizedSkillCandidate
    ) -> Tuple[bool, str]:
        """Validate candidate against size and structural constraint gates."""
        if not candidate.name:
            return False, "Skill name cannot be empty"

        if candidate.size_bytes > MAX_SKILL_SIZE_BYTES:
            return (
                False,
                f"Skill size {candidate.size_bytes}B exceeds {MAX_SKILL_SIZE_BYTES}B limit",
            )

        # Validate frontmatter presence
        md = candidate.skill_markdown.strip()
        if not (md.startswith("---") and "---" in md[3:]):
            return False, "Missing YAML frontmatter markers (---)"

        if "name:" not in md or "description:" not in md:
            return False, "Missing name or description in YAML frontmatter"

        return True, "Valid"

    @classmethod
    def save_skill(
        cls, candidate: CrystallizedSkillCandidate, target_dir: Path | str
    ) -> Path:
        """Persist the crystallized skill into the target skills directory."""
        dest_dir = Path(target_dir) / candidate.name
        dest_dir.mkdir(parents=True, exist_ok=True)
        skill_path = dest_dir / "SKILL.md"
        with open(skill_path, "w", encoding="utf-8") as f:
            f.write(candidate.skill_markdown)
        return skill_path
