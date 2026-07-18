#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Bi-level RSI evaluation protocol (issue #1166).

Adopts the AIDE² evaluation discipline (Weco AI, 2026) for the evolution
pipeline's go/no-go decision:

1. **Public/private score split** — the implementer/analysis stage sees only the
   *public* cases; the go/no-go gate decides on a *private* held-out set. A change
   that beats public but regresses private is rejected as suspected reward-hacking
   (directly complements ``evolution_reward_hacking_diagnosis`` #1165).
2. **Fixed cost budget** — each cycle runs under a hard token/cost cap; a change
   that only wins by spending more than the budget is rejected. Selection pressure
   toward algorithmic improvement over brute-force compute.
3. **Task heterogeneity** — eval spans multiple task classes; a change that helps
   one class but regresses an adjacent class is rejected (must generalize).

Design: pure, deterministic, standard-library only, no side effects on import.
This module owns the *decision discipline* — it consumes already-computed per-case
scores (from the existing eval infra) and applies the bi-level rule; it does not
run the eval itself.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Sequence

__all__ = [
    "Split",
    "EvalCase",
    "CostBudget",
    "BiLevelDecision",
    "partition_scores",
    "bilevel_decision",
    "evaluate",
    "main",
]


class Split(str, Enum):
    public = "public"
    private = "private"


@dataclass(frozen=True)
class EvalCase:
    id: str
    task_class: str = "default"
    split: Split = Split.public

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "EvalCase":
        return cls(
            id=str(d["id"]),
            task_class=str(d.get("task_class", "default")),
            split=Split(d.get("split", "public")),
        )


@dataclass(frozen=True)
class CostBudget:
    max_tokens: float
    spent: float = 0.0

    @property
    def over_budget(self) -> bool:
        return self.spent > self.max_tokens

    def to_dict(self) -> dict[str, Any]:
        return {"max_tokens": self.max_tokens, "spent": self.spent, "over_budget": self.over_budget}


@dataclass(frozen=True)
class BiLevelDecision:
    go: bool
    reason: str
    public_delta: float
    private_delta: float
    per_class_delta: dict[str, float] = field(default_factory=dict)
    reward_hacking_suspected: bool = False
    over_budget: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "go": self.go,
            "reason": self.reason,
            "public_delta": round(self.public_delta, 4),
            "private_delta": round(self.private_delta, 4),
            "per_class_delta": {k: round(v, 4) for k, v in self.per_class_delta.items()},
            "reward_hacking_suspected": self.reward_hacking_suspected,
            "over_budget": self.over_budget,
        }


def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def partition_scores(
    cases: Sequence[EvalCase],
    scores: Mapping[str, float],
) -> tuple[dict[str, float], dict[str, float]]:
    """Split a flat ``{case_id: score}`` mapping into (public, private) by case split."""
    public: dict[str, float] = {}
    private: dict[str, float] = {}
    for case in cases:
        if case.id not in scores:
            continue
        (public if case.split is Split.public else private)[case.id] = float(scores[case.id])
    return public, private


def bilevel_decision(
    cases: Sequence[EvalCase],
    candidate_scores: Mapping[str, float],
    incumbent_scores: Mapping[str, float],
    budget: CostBudget,
    *,
    min_private_gain: float = 0.0,
    max_class_regression: float = 0.0,
) -> BiLevelDecision:
    """Apply the bi-level go/no-go rule (higher score is better).

    Reject if: over budget; OR the private held-out mean does not beat the
    incumbent by ``min_private_gain``; OR the candidate beats public but regresses
    private (suspected reward-hacking); OR any task class regresses beyond
    ``max_class_regression``.
    """
    cand_pub, cand_priv = partition_scores(cases, candidate_scores)
    inc_pub, inc_priv = partition_scores(cases, incumbent_scores)

    public_delta = _mean(list(cand_pub.values())) - _mean(list(inc_pub.values()))
    private_delta = _mean(list(cand_priv.values())) - _mean(list(inc_priv.values()))

    # Per-class deltas (all cases, both splits — a change must generalize).
    class_ids: dict[str, list[str]] = {}
    for c in cases:
        class_ids.setdefault(c.task_class, []).append(c.id)
    per_class_delta: dict[str, float] = {}
    for cls, ids in class_ids.items():
        cand = [float(candidate_scores[i]) for i in ids if i in candidate_scores]
        inc = [float(incumbent_scores[i]) for i in ids if i in incumbent_scores]
        per_class_delta[cls] = _mean(cand) - _mean(inc)

    reward_hacking = public_delta > 0 and private_delta < 0
    regressed_class = min(per_class_delta.values()) if per_class_delta else 0.0

    if budget.over_budget:
        return BiLevelDecision(False, "over cost budget", public_delta, private_delta,
                               per_class_delta, reward_hacking, True)
    if reward_hacking:
        return BiLevelDecision(False, "beats public but regresses private — suspected reward-hacking",
                               public_delta, private_delta, per_class_delta, True, False)
    if private_delta <= 0 or private_delta < min_private_gain:
        return BiLevelDecision(False, f"private held-out gain {private_delta:.4f} below required {min_private_gain}",
                               public_delta, private_delta, per_class_delta, False, False)
    if regressed_class < -abs(max_class_regression):
        worst = min(per_class_delta, key=per_class_delta.get)
        return BiLevelDecision(False, f"task class '{worst}' regressed ({per_class_delta[worst]:.4f}) — not generalizable",
                               public_delta, private_delta, per_class_delta, False, False)
    return BiLevelDecision(True, "beats incumbent on private held-out set under budget, no class regression",
                           public_delta, private_delta, per_class_delta, False, False)


def evaluate(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Core entry from a JSON payload: {cases, candidate_scores, incumbent_scores, budget, ...}."""
    cases = [EvalCase.from_dict(c) for c in payload.get("cases", [])]
    budget_raw = payload.get("budget", {})
    budget = CostBudget(
        max_tokens=float(budget_raw.get("max_tokens", float("inf"))),
        spent=float(budget_raw.get("spent", 0.0)),
    )
    decision = bilevel_decision(
        cases,
        {str(k): float(v) for k, v in payload.get("candidate_scores", {}).items()},
        {str(k): float(v) for k, v in payload.get("incumbent_scores", {}).items()},
        budget,
        min_private_gain=float(payload.get("min_private_gain", 0.0)),
        max_class_regression=float(payload.get("max_class_regression", 0.0)),
    )
    return {"decision": decision.to_dict(), "budget": budget.to_dict()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bi-level RSI eval decision (#1166)")
    parser.add_argument("--payload", required=True, help="path to the eval payload JSON")
    args = parser.parse_args(argv)
    with open(args.payload, encoding="utf-8") as fh:
        payload = json.load(fh)
    result = evaluate(payload)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["decision"]["go"] else 1


if __name__ == "__main__":
    sys.exit(main())
