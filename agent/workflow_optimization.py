# -*- coding: utf-8 -*-
"""Hybrid workflow optimization recipe: select > generate > edit (#2254).

Implements the hybrid workflow recipe from 'From Static Templates to Dynamic Runtime
Graphs' (arXiv:2603.22386 §7.5):
1. SELECT (Tier 1, lowest cost): Prioritize selecting from existing static scaffolds,
   skills, and proven delegation patterns.
2. GENERATE (Tier 2, medium cost): Generate new node/subagent configurations or tool
   compositions only when matching static assets are unavailable or fail.
3. EDIT (Tier 3, highest cost): In-execution dynamic plan/graph modifications are
   strictly reserved for runtime anomalies and genuine uncertainty.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Mapping, Sequence


class OptimizationTier(str, Enum):
    """Workflow plasticity hierarchy ordered by increasing operational cost."""

    SELECT = "select"
    GENERATE = "generate"
    EDIT = "edit"


@dataclass(frozen=True)
class WorkflowRoutingDecision:
    """Decision produced by the hybrid workflow optimizer."""

    tier: OptimizationTier
    asset_type: str | None = None  # "skill", "delegation_pattern", or None
    asset_id: str | None = None  # name of skill or pattern slug
    confidence: float = 0.0
    reasoning: str = ""
    in_execution_edit_allowed: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["tier"] = self.tier.value
        return d


def route_workflow(
    task: str,
    available_skills: Sequence[Any] = (),
    available_delegations: Sequence[Any] = (),
    *,
    uncertainty: float = 0.0,
    is_in_flight: bool = False,
    min_select_confidence: float = 0.5,
) -> WorkflowRoutingDecision:
    """Apply the select-before-generate-before-edit workflow optimization recipe.

    Parameters
    ----------
    task : str
        The user task or sub-task prompt.
    available_skills : Sequence[Any]
        List of known skills (duck-typed with .name / .description or mappings).
    available_delegations : Sequence[Any]
        List of known DelegationPattern objects from the agent experience bank.
    uncertainty : float
        Current uncertainty / ambiguity score (0.0 = completely certain, 1.0 = highly uncertain).
    is_in_flight : bool
        Whether this decision is happening mid-execution vs before execution starts.
    min_select_confidence : float
        Threshold above which existing assets are selected without generation.
    """
    task_lower = (task or "").lower().strip()

    # Rule 3: In-execution editing is reserved strictly for genuine uncertainty mid-flight
    if is_in_flight:
        if uncertainty >= 0.7:
            return WorkflowRoutingDecision(
                tier=OptimizationTier.EDIT,
                confidence=round(uncertainty, 2),
                reasoning="High in-flight uncertainty/anomaly detected — escalated to dynamic graph edit",
                in_execution_edit_allowed=True,
                metadata={"uncertainty": uncertainty, "in_flight": True},
            )
        return WorkflowRoutingDecision(
            tier=OptimizationTier.SELECT,
            confidence=round(1.0 - uncertainty, 2),
            reasoning="In-flight execution stable — maintaining existing execution path without edit",
            in_execution_edit_allowed=False,
            metadata={"uncertainty": uncertainty, "in_flight": True},
        )

    # Rule 1: Always try to SELECT from existing proven assets first
    # Check proven delegation patterns
    best_delegation = None
    best_del_score = 0.0
    for del_pat in available_delegations:
        pat_type = getattr(del_pat, "task_type", "") or (
            del_pat.get("task_type") if isinstance(del_pat, Mapping) else ""
        )
        success_rate = getattr(del_pat, "success_rate", 0.0) or (
            del_pat.get("success_rate", 0.0) if isinstance(del_pat, Mapping) else 0.0
        )
        if pat_type and (
            pat_type.lower() in task_lower or task_lower in pat_type.lower()
        ):
            score = float(success_rate)
            if score > best_del_score:
                best_del_score = score
                best_delegation = del_pat

    if best_delegation and best_del_score >= min_select_confidence:
        role = getattr(best_delegation, "role", "") or (
            best_delegation.get("role") if isinstance(best_delegation, Mapping) else ""
        )
        task_t = getattr(best_delegation, "task_type", "") or (
            best_delegation.get("task_type")
            if isinstance(best_delegation, Mapping)
            else ""
        )
        return WorkflowRoutingDecision(
            tier=OptimizationTier.SELECT,
            asset_type="delegation_pattern",
            asset_id=f"{task_t}:{role}",
            confidence=round(best_del_score, 2),
            reasoning=f"Matched proven delegation pattern '{task_t}' with {round(best_del_score * 100)}% historical success rate",
            in_execution_edit_allowed=False,
        )

    # Check existing skills
    best_skill = None
    best_skill_score = 0.0
    for sk in available_skills:
        name = getattr(sk, "name", "") or (
            sk.get("name") if isinstance(sk, Mapping) else ""
        )
        desc = getattr(sk, "description", "") or (
            sk.get("description") if isinstance(sk, Mapping) else ""
        )
        name_lower = str(name).lower()
        desc_lower = str(desc).lower()

        # Word token overlap
        words = [
            w
            for w in name_lower.replace("-", " ").replace("_", " ").split()
            if len(w) > 2
        ]
        matches = sum(1 for w in words if w in task_lower)
        if words and matches > 0:
            score = matches / len(words)
            if score > best_skill_score:
                best_skill_score = score
                best_skill = str(name)
        elif name_lower and name_lower in task_lower:
            best_skill_score = 1.0
            best_skill = str(name)
            break

    if best_skill and best_skill_score >= min_select_confidence:
        return WorkflowRoutingDecision(
            tier=OptimizationTier.SELECT,
            asset_type="skill",
            asset_id=best_skill,
            confidence=round(best_skill_score, 2),
            reasoning=f"Selected existing skill '{best_skill}' matching task intent",
            in_execution_edit_allowed=False,
        )

    # Rule 2: If selection cannot satisfy the requirement, fall back to GENERATE
    return WorkflowRoutingDecision(
        tier=OptimizationTier.GENERATE,
        confidence=round(1.0 - uncertainty, 2),
        reasoning="No high-confidence static skill or delegation pattern found — generating dynamic workflow plan",
        in_execution_edit_allowed=False,
        metadata={"uncertainty": uncertainty},
    )
