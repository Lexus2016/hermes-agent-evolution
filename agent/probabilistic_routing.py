"""Training-free probabilistic routing and selection for recursive delegation.

Implements #3285: Thompson sampling and belief-guided controller for multi-agent
delegation and model selection (REDEREF-inspired).
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class AgentBelief:
    """Beta distribution parameters representing beliefs about an agent/model performance."""

    alpha: float = 1.0  # prior successes
    beta: float = 1.0   # prior failures
    task_priors: dict[str, tuple[float, float]] = field(default_factory=dict)

    def sample(self, task_type: str = "general", rng: Optional[random.Random] = None) -> float:
        """Sample expected marginal contribution via Thompson sampling (Beta distribution)."""
        a, b = self.alpha, self.beta
        if task_type in self.task_priors:
            pa, pb = self.task_priors[task_type]
            a += pa
            b += pb
        _rng = rng or random
        return _rng.betavariate(max(0.01, a), max(0.01, b))

    def expected_value(self, task_type: str = "general") -> float:
        """Calculate mean expected contribution."""
        a, b = self.alpha, self.beta
        if task_type in self.task_priors:
            pa, pb = self.task_priors[task_type]
            a += pa
            b += pb
        return a / (a + b)

    def update(self, success: bool, weight: float = 1.0, task_type: str = "general") -> None:
        """Update posterior belief after an observed execution outcome."""
        inc = max(0.1, float(weight))
        if success:
            self.alpha += inc
        else:
            self.beta += inc

        if task_type and task_type != "general":
            pa, pb = self.task_priors.get(task_type, (0.0, 0.0))
            if success:
                self.task_priors[task_type] = (pa + inc, pb)
            else:
                self.task_priors[task_type] = (pa, pb + inc)


class ProbabilisticRouter:
    """Probabilistic controller for selecting and re-routing delegation targets."""

    def __init__(self, rng: Optional[random.Random] = None):
        self.beliefs: dict[str, AgentBelief] = {}
        self.rng = rng or random.Random()

    def get_or_create_belief(self, target_id: str) -> AgentBelief:
        if target_id not in self.beliefs:
            self.beliefs[target_id] = AgentBelief()
        return self.beliefs[target_id]

    def select_target(
        self,
        candidates: list[str],
        task_type: str = "general",
        exclude: Optional[list[str]] = None,
    ) -> str:
        """Select target candidate using Thompson sampling with belief-guided priors."""
        if not candidates:
            raise ValueError("candidates list must not be empty")

        available = [c for c in candidates if not exclude or c not in exclude]
        if not available:
            available = candidates  # fallback if all excluded

        samples = {}
        for cand in available:
            belief = self.get_or_create_belief(cand)
            samples[cand] = belief.sample(task_type=task_type, rng=self.rng)

        # Pick candidate with highest sampled value
        return max(samples, key=samples.get)

    def record_outcome(
        self,
        target_id: str,
        success: bool,
        weight: float = 1.0,
        task_type: str = "general",
    ) -> None:
        """Record outcome and update posterior belief."""
        belief = self.get_or_create_belief(target_id)
        belief.update(success=success, weight=weight, task_type=task_type)

    def to_dict(self) -> dict[str, Any]:
        """Serialize beliefs to dict for persistence."""
        return {
            target: {
                "alpha": b.alpha,
                "beta": b.beta,
                "task_priors": b.task_priors,
            }
            for target, b in self.beliefs.items()
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProbabilisticRouter:
        """Restore beliefs from dict."""
        router = cls()
        for target, d in data.items():
            belief = AgentBelief(
                alpha=d.get("alpha", 1.0),
                beta=d.get("beta", 1.0),
                task_priors={
                    k: tuple(v) for k, v in d.get("task_priors", {}).items()
                },
            )
            router.beliefs[target] = belief
        return router


# Global shared router instance
_GLOBAL_ROUTER: Optional[ProbabilisticRouter] = None


def get_global_probabilistic_router() -> ProbabilisticRouter:
    global _GLOBAL_ROUTER
    if _GLOBAL_ROUTER is None:
        _GLOBAL_ROUTER = ProbabilisticRouter()
    return _GLOBAL_ROUTER


def route_delegation_target(
    candidates: list[str],
    task_type: str = "general",
    exclude: Optional[list[str]] = None,
    router: Optional[ProbabilisticRouter] = None,
) -> str:
    """Convenience helper to select a delegation target using Thompson sampling."""
    r = router or get_global_probabilistic_router()
    return r.select_target(candidates, task_type=task_type, exclude=exclude)
