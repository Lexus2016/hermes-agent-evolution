# -*- coding: utf-8 -*-
"""Trajectory-adapted uncertainty quantification for evolution decisions (#2386).

Inspired by arXiv:2608.11552 ("Trajectory-Adapted Uncertainty Quantification:
Single-Turn Confidence Does Not Transfer to Agent Trajectories"):
- Single-turn confidence metrics (e.g. token logprobs) do not reliably transfer
  to multi-turn agent trajectories.
- Uses reflexive P(True) as a low-cost baseline for finding confidence.
- Uses Trajectory Equivalence Rate (TER) across multiple runs/evaluations for
  high-stakes pipeline decisions.
- Maps estimated confidence to clear operational actions:
  proceed_to_implementation, second_research_pass, or defer.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Sequence


@dataclass
class TrajectoryConfidenceAssessment:
    """Uncertainty quantification assessment for an evolution pipeline decision."""

    score: int  # 0-100 scale
    method: str  # "reflexive_p_true", "trajectory_equivalence_rate", "hybrid"
    p_true: float  # 0.0 - 1.0
    ter: float | None = None  # 0.0 - 1.0 when multiple trajectories compared
    verdict: str = (
        "medium_confidence"  # "high_confidence", "medium_confidence", "low_confidence"
    )
    action: str = "second_research_pass"  # "proceed_to_implementation", "second_research_pass", "defer"
    evidence_count: int = 0
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TrajectoryConfidenceAssessment:
        return cls(
            score=int(data.get("score", 0)),
            method=str(data.get("method", "reflexive_p_true")),
            p_true=float(data.get("p_true", 0.0)),
            ter=float(data["ter"]) if data.get("ter") is not None else None,
            verdict=str(data.get("verdict", "medium_confidence")),
            action=str(data.get("action", "second_research_pass")),
            evidence_count=int(data.get("evidence_count", 0)),
            details=dict(data.get("details", {})),
        )


def compute_trajectory_equivalence_rate(
    outcomes: Sequence[Any],
) -> float:
    """Compute the Trajectory Equivalence Rate (TER) across trajectory outcomes.

    TER measures the proportion of trajectory pairs that reached equivalent outcomes
    (arXiv:2608.11552). Returns a float between 0.0 and 1.0.
    """
    if not outcomes:
        return 0.0
    if len(outcomes) == 1:
        return 1.0

    # Canonicalize outcomes to comparable representations
    canonical: list[str] = []
    for item in outcomes:
        if isinstance(item, dict):
            # Normalize dict key ordering
            canonical.append(json.dumps(item, sort_keys=True, default=str))
        elif isinstance(item, (list, set, tuple)):
            canonical.append(json.dumps(sorted(str(x) for x in item)))
        else:
            canonical.append(str(item).strip().lower())

    total_pairs = 0
    matching_pairs = 0
    n = len(canonical)
    for i in range(n):
        for j in range(i + 1, n):
            total_pairs += 1
            if canonical[i] == canonical[j]:
                matching_pairs += 1

    return matching_pairs / total_pairs if total_pairs > 0 else 0.0


def estimate_reflexive_p_true(
    finding: dict[str, Any],
) -> float:
    """Estimate reflexive P(True) for a finding based on evidence and structure.

    Evaluates explicit confidence markers, evidence pointer density,
    reproducibility indicators, and absence of contradictory signals.
    """
    # 1. Base prior from explicit confidence if available
    raw_conf = finding.get("confidence")
    if isinstance(raw_conf, (int, float)):
        base = max(
            0.0,
            min(1.0, float(raw_conf) / 100.0 if raw_conf > 1.0 else float(raw_conf)),
        )
    else:
        base = 0.5

    # 2. Evidence density adjustment
    evidence = finding.get("evidence_pointers", []) or finding.get("evidence", [])
    ev_count = (
        len(evidence)
        if isinstance(evidence, (list, tuple, set))
        else (1 if evidence else 0)
    )

    if ev_count >= 3:
        evidence_bonus = 0.2
    elif ev_count >= 1:
        evidence_bonus = 0.1
    else:
        evidence_bonus = -0.15

    # 3. Reproducibility & verification flag
    is_verified = bool(
        finding.get("verified", False) or finding.get("reproduced", False)
    )
    verified_bonus = 0.15 if is_verified else 0.0

    # 4. Uncertainty markers in summary/text
    text = str(finding.get("summary", "") or finding.get("description", "")).lower()
    uncertainty_penalty = 0.0
    if any(
        w in text
        for w in (
            "maybe",
            "perhaps",
            "unclear",
            "unverified",
            "guess",
            "likely",
            "tentative",
        )
    ):
        uncertainty_penalty = -0.1

    p_true = base + evidence_bonus + verified_bonus + uncertainty_penalty
    return max(0.0, min(1.0, round(p_true, 3)))


def assess_finding_confidence(
    finding: dict[str, Any],
    trajectories: Sequence[Any] | None = None,
    *,
    threshold_high: int = 75,
    threshold_low: int = 40,
) -> TrajectoryConfidenceAssessment:
    """Compute trajectory-adapted uncertainty quantification for an evolution finding.

    - High-confidence (>= threshold_high): proceeds directly to implementation.
    - Low-confidence (< threshold_low): deferred / abstained.
    - Medium-confidence: scheduled for a second research pass.
    """
    p_true = estimate_reflexive_p_true(finding)
    evidence = finding.get("evidence_pointers", []) or finding.get("evidence", [])
    ev_count = (
        len(evidence)
        if isinstance(evidence, (list, tuple, set))
        else (1 if evidence else 0)
    )

    ter: float | None = None
    if trajectories and len(trajectories) >= 2:
        ter = compute_trajectory_equivalence_rate(trajectories)
        method = "hybrid"
        # Combine reflexive P(True) and empirical TER
        combined_score = int(round((0.4 * p_true + 0.6 * ter) * 100))
    else:
        method = "reflexive_p_true"
        combined_score = int(round(p_true * 100))

    score = max(0, min(100, combined_score))

    if score >= threshold_high:
        verdict = "high_confidence"
        action = "proceed_to_implementation"
    elif score < threshold_low:
        verdict = "low_confidence"
        action = "defer"
    else:
        verdict = "medium_confidence"
        action = "second_research_pass"

    return TrajectoryConfidenceAssessment(
        score=score,
        method=method,
        p_true=p_true,
        ter=ter,
        verdict=verdict,
        action=action,
        evidence_count=ev_count,
        details={
            "threshold_high": threshold_high,
            "threshold_low": threshold_low,
            "has_trajectories": bool(trajectories and len(trajectories) >= 2),
        },
    )
