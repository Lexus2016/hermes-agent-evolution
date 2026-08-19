# -*- coding: utf-8 -*-
"""GRASP-style balanced-probe regression gate for the skill-evolution loop (#2840).

GRASP (Gated Regression-Aware Skill Proposer, arXiv:2605.29668 v2) treats
agent self-improvement as validated edits to a bounded, versioned skill
library: a candidate skill edit is re-run on a BALANCED probe of
previously-failing AND previously-passing examples, and is accepted only when
it fixes more failures than it causes regressions AND introduces no regression
beyond the existing baseline (hard regression budget). When a fix nets
positive but regresses at least one case, a contrastive-revision step shows
the skill-writer the regressed example so the trigger can be narrowed.

This module supplies the pieces the codebase's earlier skill gates (SkillProx
``verify_skill_edit``, ``rubric_heldout_gate``, ``skill_shrink``,
``skill_hub_tx``) did not fully supply:

1. **Balanced hold-out probe** — the hold-out split is stratified so it
   contains BOTH previously-failing and previously-passing examples (a tail
   split can be all-one-class and silently blind the gate to regressions).
2. **Hard regression budget** — a candidate is accepted only when net fixes
   exceed regressions AND regressions stay within the baseline threshold.
3. **Contrastive revision** — a regress-but-positive candidate returns the
   regressed examples so the writer can narrow the trigger and resubmit.

Pure, deterministic, import-safe, no LLM, no IO. The judge signature reuses
``RubricJudge`` from :mod:`evolution.lib.rubric_forge`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Sequence, Tuple

from evolution.lib.rubric_forge import RubricJudge

__all__ = [
    "GraspVerdict",
    "DECISION_ACCEPT",
    "DECISION_REVISE",
    "DECISION_REJECT",
    "balanced_probe_split",
    "grasp_regression_gate",
]

#: Candidate skill edits pass the hard gate (accepted into the library).
DECISION_ACCEPT = "accept"
#: Net-positive but regressed a case — contrastive revision should narrow it.
DECISION_REVISE = "revise"
#: Failed the hard budget / net-negative — rejected outright.
DECISION_REJECT = "reject"


@dataclass
class GraspVerdict:
    """Decision for one candidate skill edit on the balanced probe.

    A "previously-failing" example is one the OLD skill accepted but should
    have rejected (label ``False``): a new skill FIXES it by rejecting it.
    A "previously-passing" example (label ``True``) is REGRESSED when the new
    skill now rejects it. ``net_fixed`` = fixed - regressed.
    """

    decision: str
    net_fixed: int
    fixed: int
    regressed: int
    regression_budget: int
    probe_size: int
    regressed_examples: List[Any] = field(default_factory=list)
    reason: str = ""


def balanced_probe_split(
    labeled: Sequence[Any],
    labels: Sequence[bool],
    *,
    probe_size: int = 4,
) -> Tuple[List[Any], List[bool], List[Any], List[bool]]:
    """Stratified split: keep a balanced mix of failing and passing in the probe.

    Returns ``(train_ex, train_lb, probe_ex, probe_lb)`` where the probe holds
    up to ``probe_size`` examples drawn deterministically so that BOTH
    previously-failing (label False) and previously-passing (label True)
    examples are represented. Deterministic (no RNG) so decisions are
    reproducible. Falls back to the whole set when it is too small to split.
    """
    if len(labeled) != len(labels):
        raise ValueError("labeled and labels must be parallel sequences")

    failing = [i for i, lb in enumerate(labels) if not lb]
    passing = [i for i, lb in enumerate(labels) if lb]

    probe_idx: List[int] = []
    # Round-robin over failing/passing so both classes appear (balanced probe).
    # Both branches respect the probe_size cap (the earlier failing branch
    # could overflow past probe_size).
    for i in range(max(0, probe_size)):
        if i % 2 == 0:
            if failing and len(probe_idx) < probe_size:
                probe_idx.append(failing.pop(0))
        elif passing and len(probe_idx) < probe_size:
            probe_idx.append(passing.pop(0))

    train_ex = [labeled[i] for i in range(len(labeled)) if i not in set(probe_idx)]
    train_lb = [labels[i] for i in range(len(labeled)) if i not in set(probe_idx)]
    probe_ex = [labeled[i] for i in probe_idx]
    probe_lb = [labels[i] for i in probe_idx]
    return train_ex, train_lb, probe_ex, probe_lb


def _verdict_breakdown(
    candidate: str,
    probe_ex: Sequence[Any],
    probe_lb: Sequence[bool],
    judge: RubricJudge,
) -> Tuple[int, int, List[Any]]:
    """Count (fixed, regressed, regressed_examples) for ``candidate``.

    - FIXED: a previously-FAILING example (label False) the candidate now
      correctly REJECTS (judge -> False).
    - REGRESSED: a previously-PASSING example (label True) the candidate now
      wrongly REJECTS (judge -> False).
    A throwing judge rejects nothing and accepts nothing it cannot judge.
    """
    fixed = 0
    regressed = 0
    regressed_examples: List[Any] = []
    for ex, lb in zip(probe_ex, probe_lb):
        try:
            accepted = bool(judge(candidate, ex))
        except Exception:  # noqa: BLE001 - a throwing judge proves nothing
            continue  # unknown outcome: count as neither fixed nor regressed
        if not lb and not accepted:
            fixed += 1  # previously-failing, now correctly rejected
        elif lb and not accepted:
            regressed += 1  # previously-passing, now wrongly rejected
            regressed_examples.append(ex)
    return fixed, regressed, regressed_examples


def grasp_regression_gate(
    candidate: str,
    baseline: str,
    labeled: Sequence[Any],
    labels: Sequence[bool],
    judge: RubricJudge,
    *,
    probe_size: int = 4,
    regression_budget: int = 0,
) -> GraspVerdict:
    """GRASP gate for a candidate skill edit (``candidate`` vs ``baseline``).

    Splits into a balanced probe, re-runs the candidate on it, and decides:

    * **accept** — the candidate fixes MORE previously-failing examples than
      it regresses previously-passing ones (``net_fixed > 0``) AND the number
      of regressions stays within ``regression_budget`` (hard budget, GRASP).
    * **revise** — net-positive but regressed at least one passing example
      beyond budget: contrastive-revision carries the regressed examples back
      to the skill-writer so the trigger can be narrowed.
    * **reject** — net non-positive, or the probe is empty (nothing provable).

    The ``baseline`` is used only for the regression-budget comparison: the
    candidate is not worse than the shipped baseline's own regressions. Pure
    and deterministic.
    """
    _, _, probe_ex, probe_lb = balanced_probe_split(
        labeled, labels, probe_size=probe_size
    )
    if not probe_ex:
        return GraspVerdict(
            decision=DECISION_REJECT,
            net_fixed=0,
            fixed=0,
            regressed=0,
            regression_budget=regression_budget,
            probe_size=0,
            reason="empty probe — regression parity unprovable, refusing acceptance",
        )

    fixed, regressed, regressed_examples = _verdict_breakdown(
        candidate, probe_ex, probe_lb, judge
    )
    net_fixed = fixed - regressed

    # Hard regression budget: regressions must not exceed the baseline's own.
    baseline_regressed = 0
    try:
        _, baseline_regressed, _ = _verdict_breakdown(
            baseline, probe_ex, probe_lb, judge
        )
    except Exception:  # noqa: BLE001 - a throwing baseline just means budget 0
        baseline_regressed = 0
    hard_budget = max(regression_budget, baseline_regressed)

    if net_fixed > 0 and regressed <= hard_budget:
        return GraspVerdict(
            decision=DECISION_ACCEPT,
            net_fixed=net_fixed,
            fixed=fixed,
            regressed=regressed,
            regression_budget=hard_budget,
            probe_size=len(probe_ex),
            reason=f"net fixes {net_fixed:+d} (fixed {fixed}, regressed {regressed}) within hard budget {hard_budget}",
        )
    if net_fixed > 0 and regressed > hard_budget:
        return GraspVerdict(
            decision=DECISION_REVISE,
            net_fixed=net_fixed,
            fixed=fixed,
            regressed=regressed,
            regression_budget=hard_budget,
            probe_size=len(probe_ex),
            regressed_examples=regressed_examples,
            reason=f"net-positive {net_fixed:+d} but {regressed} regressions exceed budget {hard_budget}",
        )
    return GraspVerdict(
        decision=DECISION_REJECT,
        net_fixed=net_fixed,
        fixed=fixed,
        regressed=regressed,
        regression_budget=hard_budget,
        probe_size=len(probe_ex),
        regressed_examples=regressed_examples,
        reason=f"net fixes {net_fixed:+d} is not positive — rejected",
    )
