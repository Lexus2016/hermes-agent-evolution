# -*- coding: utf-8 -*-
"""Sub-skill schema for cross-task skill transfer (issue #3070).

A sub-skill is a reusable primitive extracted from a completed task.  It is
coarser than a single tool call and finer than a full SKILL.md bundle.  The
schema is intentionally minimal so it can be round-tripped through the
existing skill-hub install/uninstall flow and written to the audit log
without inventing a new storage layer.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

__all__ = [
    "SubSkill",
    "CapabilityFragment",
    "Precondition",
    "SubSkillProvenance",
    "SubSkillStatus",
    "validate_subskill",
    "subskill_to_bundle_meta",
]


class SubSkillStatus:
    """Lifecycle states for a sub-skill promotion."""

    EXTRACTED = "extracted"  # candidate produced by the extractor
    PENDING_REVIEW = "pending_review"  # awaiting human approval
    APPROVED = "approved"  # cleared for reuse
    REJECTED = "rejected"  # explicitly turned down
    RETIRED = "retired"  # no longer offered for reuse


@dataclass
class Precondition:
    """A simple structured matcher for when a sub-skill applies."""

    intent_keywords: List[str] = field(default_factory=list)
    required_tool_names: List[str] = field(default_factory=list)
    required_state_keys: List[str] = field(default_factory=list)
    description: str = ""

    def match_score(
        self, intent: str, tool_names: List[str], state_keys: List[str]
    ) -> float:
        """Return a 0..1 overlap score against a planning context."""
        intent = intent.lower()
        scores: List[float] = []
        if self.intent_keywords:
            hits = sum(1 for kw in self.intent_keywords if kw.lower() in intent)
            scores.append(hits / len(self.intent_keywords))
        if self.required_tool_names:
            hits = len(set(self.required_tool_names) & set(tool_names))
            scores.append(hits / len(self.required_tool_names))
        if self.required_state_keys:
            hits = len(set(self.required_state_keys) & set(state_keys))
            scores.append(hits / len(self.required_state_keys))
        if not scores:
            return 0.0
        return sum(scores) / len(scores)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class CapabilityFragment:
    """The reusable 'how' of a sub-skill: a tool sequence plus parameters."""

    description: str = ""
    tool_sequence: List[str] = field(default_factory=list)
    parameter_schema: Dict[str, Any] = field(default_factory=dict)
    example_usage: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class SubSkillProvenance:
    """Where a sub-skill came from and when it changed."""

    source_task_id: str = ""
    source_session_id: str = ""
    extracted_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    extractor_version: str = "1.0"
    human_reviewer: Optional[str] = None
    reviewed_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class SubSkill:
    """A reusable primitive extracted from a successful task trace."""

    name: str
    description: str
    precondition: Precondition = field(default_factory=Precondition)
    capability: CapabilityFragment = field(default_factory=CapabilityFragment)
    provenance: SubSkillProvenance = field(default_factory=SubSkillProvenance)
    status: str = SubSkillStatus.EXTRACTED
    review_required: bool = True
    tags: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    subskill_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("sub-skill name is required")
        # Normalize to a filesystem- and URL-friendly identifier.
        self._id_slug = re.sub(r"[^a-z0-9_-]+", "-", self.name.lower()).strip("-")

    @property
    def id_slug(self) -> str:
        return getattr(self, "_id_slug", "")

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a plain JSON-compatible dict."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SubSkill":
        """Deserialize from a plain dict; ignores unknown fields."""
        data = dict(data)
        data.pop("_id_slug", None)
        data["precondition"] = Precondition(**data.get("precondition", {}))
        data["capability"] = CapabilityFragment(**data.get("capability", {}))
        data["provenance"] = SubSkillProvenance(**data.get("provenance", {}))
        return cls(**{
            k: v
            for k, v in data.items()
            if k in {f.name for f in cls.__dataclass_fields__.values()}
        })

    def approve(self, reviewer: str) -> None:
        """Promote the sub-skill to approved status after human review."""
        self.status = SubSkillStatus.APPROVED
        self.review_required = False
        self.provenance.human_reviewer = reviewer
        self.provenance.reviewed_at = datetime.now(timezone.utc).isoformat()

    def reject(self, reviewer: str) -> None:
        """Mark the sub-skill as rejected by human review."""
        self.status = SubSkillStatus.REJECTED
        self.review_required = False
        self.provenance.human_reviewer = reviewer
        self.provenance.reviewed_at = datetime.now(timezone.utc).isoformat()


def validate_subskill(skill: SubSkill) -> Dict[str, Any]:
    """Return a validation report with ``valid`` and a list of errors."""
    errors: List[str] = []
    if not skill.name or not skill.name.strip():
        errors.append("name is empty")
    if not skill.description or not skill.description.strip():
        errors.append("description is empty")
    if (
        not skill.precondition.intent_keywords
        and not skill.precondition.required_tool_names
    ):
        errors.append("precondition has no intent keywords or required tools")
    if not skill.capability.tool_sequence:
        errors.append("capability.tool_sequence is empty")
    if skill.status not in {
        SubSkillStatus.EXTRACTED,
        SubSkillStatus.PENDING_REVIEW,
        SubSkillStatus.APPROVED,
        SubSkillStatus.REJECTED,
        SubSkillStatus.RETIRED,
    }:
        errors.append(f"unknown status {skill.status!r}")
    return {"valid": not errors, "errors": errors}


def subskill_to_bundle_meta(skill: SubSkill) -> Dict[str, Any]:
    """Convert a sub-skill into the metadata shape used by ``tools.skills_hub.SkillBundle``.

    This keeps the sub-skill compatible with the existing skill-hub install and
    uninstall paths: it can be stored as a single SKILL.md-less bundle entry or
    promoted into a full skill card later.
    """
    return {
        "name": skill.name,
        "description": skill.description,
        "source": "subskill",
        "identifier": f"subskill/{skill.subskill_id}/{skill.id_slug}",
        "trust_level": "agent",
        "tags": list(skill.tags),
        "extra": {
            "subskill_id": skill.subskill_id,
            "status": skill.status,
            "review_required": skill.review_required,
            "precondition": skill.precondition.to_dict(),
            "capability": skill.capability.to_dict(),
            "provenance": skill.provenance.to_dict(),
        },
    }
