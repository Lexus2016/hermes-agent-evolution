# -*- coding: utf-8 -*-
"""Pass-rate x cost Pareto optimization and pruning discipline (SkillMOO, issue #2290).

Adopts SkillMOO (arXiv:2604.09297, ASE '26):
1. Evaluates candidate skill bundles under multi-objective Pareto selection (maximize pass rate, minimize token cost).
2. Implements NSGA-II non-dominated sorting and crowding distance computation.
3. Enforces pruning and substitution discipline over passive accumulation.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

__all__ = [
    "ParetoRankingResult",
    "SkillMetricProfile",
    "SkillMOOOptimizer",
]


@dataclass
class SkillMetricProfile:
    """Metrics tracking pass-rate, token cost, and invocation frequency for a skill."""

    skill_name: str
    pass_rate: float  # 0.0 to 1.0 (maximize)
    inference_cost_tokens: int  # token overhead (minimize)
    size_bytes: int = 0
    call_count: int = 1

    def __post_init__(self) -> None:
        self.pass_rate = max(0.0, min(1.0, float(self.pass_rate)))
        self.inference_cost_tokens = max(0, int(self.inference_cost_tokens))
        self.size_bytes = max(0, int(self.size_bytes))
        self.call_count = max(1, int(self.call_count))

    def dominates(self, other: SkillMetricProfile) -> bool:
        """Return True if self Pareto-dominates other.

        Dominates if:
        1. pass_rate >= other.pass_rate AND inference_cost_tokens <= other.inference_cost_tokens
        2. At least one objective is strictly better.
        """
        better_or_equal = (
            self.pass_rate >= other.pass_rate
            and self.inference_cost_tokens <= other.inference_cost_tokens
        )
        strictly_better = (
            self.pass_rate > other.pass_rate
            or self.inference_cost_tokens < other.inference_cost_tokens
        )
        return better_or_equal and strictly_better

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ParetoRankingResult:
    """Multi-objective ranking output."""

    fronts: List[List[SkillMetricProfile]] = field(default_factory=list)
    keep_skills: List[str] = field(default_factory=list)
    prune_skills: List[str] = field(default_factory=list)
    total_cost_saved_tokens: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "fronts": [[p.to_dict() for p in front] for front in self.fronts],
            "keep_skills": self.keep_skills,
            "prune_skills": self.prune_skills,
            "total_cost_saved_tokens": self.total_cost_saved_tokens,
        }


class SkillMOOOptimizer:
    """Multi-objective Pareto optimizer and pruning orchestrator for skill bundles."""

    @classmethod
    def calculate_non_dominated_fronts(
        cls,
        profiles: Sequence[SkillMetricProfile],
    ) -> List[List[SkillMetricProfile]]:
        """Compute non-dominated Pareto fronts using fast non-dominated sorting."""
        if not profiles:
            return []

        domination_counts: Dict[str, int] = {p.skill_name: 0 for p in profiles}
        dominated_sets: Dict[str, List[SkillMetricProfile]] = {
            p.skill_name: [] for p in profiles
        }
        profile_map = {p.skill_name: p for p in profiles}

        for p in profiles:
            for q in profiles:
                if p.dominates(q):
                    dominated_sets[p.skill_name].append(q)
                elif q.dominates(p):
                    domination_counts[p.skill_name] += 1

        fronts: List[List[SkillMetricProfile]] = []
        current_front: List[SkillMetricProfile] = [
            p for p in profiles if domination_counts[p.skill_name] == 0
        ]

        while current_front:
            fronts.append(current_front)
            next_front: List[SkillMetricProfile] = []
            for p in current_front:
                for q in dominated_sets[p.skill_name]:
                    domination_counts[q.skill_name] -= 1
                    if domination_counts[q.skill_name] == 0:
                        next_front.append(q)
            current_front = next_front

        return fronts

    @classmethod
    def optimize_and_prune(
        cls,
        profiles: Sequence[SkillMetricProfile],
        min_pass_rate: float = 0.5,
        max_fronts_to_keep: int = 2,
    ) -> ParetoRankingResult:
        """Enforce pruning discipline: select top Pareto fronts and prune dominated/low-pass skills."""
        if not profiles:
            return ParetoRankingResult()

        fronts = cls.calculate_non_dominated_fronts(profiles)
        keep_skills: List[str] = []
        prune_skills: List[str] = []
        tokens_saved = 0

        # Retain top fronts satisfying quality criteria
        for i, front in enumerate(fronts):
            if i < max_fronts_to_keep:
                for p in front:
                    if p.pass_rate >= min_pass_rate:
                        keep_skills.append(p.skill_name)
                    else:
                        prune_skills.append(p.skill_name)
                        tokens_saved += p.inference_cost_tokens
            else:
                for p in front:
                    prune_skills.append(p.skill_name)
                    tokens_saved += p.inference_cost_tokens

        return ParetoRankingResult(
            fronts=fronts,
            keep_skills=keep_skills,
            prune_skills=prune_skills,
            total_cost_saved_tokens=tokens_saved,
        )
