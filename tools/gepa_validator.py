#!/usr/bin/env python3
"""GEPA held-out validation gate (issue #2232, Slice C).

Validates mutated candidates against an unseen held-out task set before
promotion, preventing overfit to training-set critiques.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

from tools.gepa_evolution import Candidate
from tools.gepa_reflector import VariantResult


@dataclass
class HeldOutResult:
    """Outcome of held-out validation for one candidate."""

    candidate_id: str
    passed: bool
    pass_rate: float
    threshold: float
    train_pass_rate: float
    n_held_out: int
    n_passed: int
    metadata: Dict[str, Any] = field(default_factory=dict)


def _pass_rate(results: List[VariantResult]) -> float:
    if not results:
        return 0.0
    return sum(1 for r in results if r.passed) / len(results)


def validate_held_out(
    candidate: Candidate,
    train_results: List[VariantResult],
    held_out_results: List[VariantResult],
    *,
    threshold: float = 0.6,
) -> HeldOutResult:
    """Validate *candidate* against a held-out task set.

    ``passed`` is ``True`` only when held-out pass-rate ≥ *threshold*.
    """
    train_rate = _pass_rate(train_results)
    held_rate = _pass_rate(held_out_results)
    n_passed = sum(1 for r in held_out_results if r.passed)
    return HeldOutResult(
        candidate_id=candidate.id,
        passed=held_rate >= threshold,
        pass_rate=held_rate,
        threshold=threshold,
        train_pass_rate=train_rate,
        n_held_out=len(held_out_results),
        n_passed=n_passed,
    )


def promote_if_valid(candidate: Candidate, result: HeldOutResult) -> bool:
    """Mark *candidate* selected (pass) or pruned (fail); record audit metadata."""
    if result.passed:
        candidate.selected = True
    else:
        candidate.pruned = True
    candidate.metadata["held_out_validation"] = {
        "passed": result.passed,
        "pass_rate": round(result.pass_rate, 4),
        "threshold": result.threshold,
        "train_pass_rate": round(result.train_pass_rate, 4),
        "n_held_out": result.n_held_out,
        "n_passed": result.n_passed,
    }
    return result.passed
