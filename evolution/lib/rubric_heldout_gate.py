# -*- coding: utf-8 -*-
"""RubricForge Slice 3 — held-out false-pass gate (#2782).

Child of #2760. An evolved rubric may overfit the labeled set it was
selected on (agreement 1.0 by construction). Before ADOPTION the winner is
re-measured on a HELD-OUT split it never saw: the gate permits adoption
only when the evolved rubric's false-pass rate on the held-out examples
does not exceed the GENERIC judge's baseline rate there.

A "false pass" is a labeled-bad example the rubric accepts (verdict True on
``label: False``) — the eval stage's failure mode RubricForge exists to
halve. Pure functions over the S1 primitives; no LLM, no IO.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Sequence, Tuple

from evolution.lib.rubric_forge import RubricJudge, score_rubric

logger = logging.getLogger(__name__)

__all__ = ["HeldOutVerdict", "split_labeled_set", "held_out_false_pass_gate"]


@dataclass
class HeldOutVerdict:
    """Adoption decision for one evolved rubric."""

    adopt: bool
    evolved_false_pass_rate: float
    baseline_false_pass_rate: float
    held_out_size: int
    reason: str


def split_labeled_set(
    labeled: Sequence[Any],
    labels: Sequence[bool],
    *,
    holdout: int = 3,
) -> Tuple[List[Any], List[bool], List[Any], List[bool]]:
    """Deterministic tail split: the LAST ``holdout`` examples are held out.

    Deterministic (no RNG) so adoption decisions are reproducible; callers
    that want randomness shuffle before calling. Falls back to an empty
    held-out set when the labeled set is too small to split.
    """
    if len(labeled) != len(labels):
        raise ValueError("labeled and labels must be parallel sequences")
    holdout = max(0, min(holdout, len(labeled) // 2))
    cut = len(labeled) - holdout
    return (
        list(labeled[:cut]),
        list(labels[:cut]),
        list(labeled[cut:]),
        list(labels[cut:]),
    )


def _false_pass_rate(
    rubric: str, held_out: Sequence[Any], labels: Sequence[bool], judge: RubricJudge
) -> float:
    """Fraction of held-out labeled-BAD examples the rubric accepts anyway."""
    bad = [(ex, lb) for ex, lb in zip(held_out, labels) if not lb]
    if not bad:
        return 0.0
    false_passes = 0
    for ex, _lb in bad:
        try:
            if bool(judge(rubric, ex)):
                false_passes += 1
        except Exception:  # noqa: BLE001 - a throwing judge accepts nothing
            pass
    return false_passes / len(bad)


def held_out_false_pass_gate(
    evolved_rubric: str,
    generic_rubric: str,
    labeled: Sequence[Any],
    labels: Sequence[bool],
    judge: RubricJudge,
    *,
    holdout: int = 3,
) -> HeldOutVerdict:
    """Gate evolved-rubric adoption on held-out false-pass parity (#2782).

    Split the labeled set, re-check the evolved winner on the held-out tail,
    and compare its false-pass rate there against the generic judge's.
    Adoption is permitted only when the evolved rate does NOT exceed the
    baseline (``<=``). An empty held-out set cannot prove parity — the gate
    refuses (fail-closed) with the reason.
    """
    _, _, held_out, held_labels = split_labeled_set(labeled, labels, holdout=holdout)
    if not held_out:
        return HeldOutVerdict(
            adopt=False,
            evolved_false_pass_rate=0.0,
            baseline_false_pass_rate=0.0,
            held_out_size=0,
            reason="held-out set empty — parity unprovable, refusing adoption",
        )
    evolved_rate = _false_pass_rate(evolved_rubric, held_out, held_labels, judge)
    baseline_rate = _false_pass_rate(generic_rubric, held_out, held_labels, judge)
    adopt = evolved_rate <= baseline_rate
    reason = (
        "evolved false-pass rate within baseline on held-out examples"
        if adopt
        else "evolved rubric false-passes MORE on held-out examples — overfit, blocked"
    )
    return HeldOutVerdict(
        adopt=adopt,
        evolved_false_pass_rate=evolved_rate,
        baseline_false_pass_rate=baseline_rate,
        held_out_size=len(held_out),
        reason=reason,
    )
