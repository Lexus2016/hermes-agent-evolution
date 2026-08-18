# -*- coding: utf-8 -*-
"""RubricForge Slice 1 — rubric-evolution primitive (#2780).

Child of #2760 (RubricForge).  A rubric is only as good as its agreement
with a labeled set of known-good / known-bad examples.  This primitive
scores a candidate rubric text against a small labeled set by *agreement
maximization*: the best rubric is the one whose verdicts match the labels
on the most examples.

Components:

1. **Rubric scorer** — a pure function that applies a candidate rubric to
   each labeled example and returns the agreement rate (fraction of examples
   where the rubric's verdict matches the label).
2. **Best-rubric selection** — given several candidate rubrics, return the
   one with the highest agreement (ties broken by the first candidate).

The rubric is applied via an injectable ``judge`` callable so the primitive
is testable without an LLM: ``judge(rubric_text, example) -> bool``.

New module, no changes to existing rubric loading.  Diff ≤ 200 lines.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "RubricScore",
    "score_rubric",
    "select_best_rubric",
]

# A judge applies a rubric text to one example and returns a bool verdict.
RubricJudge = Callable[[str, Any], bool]


@dataclass
class RubricScore:
    """Agreement of one candidate rubric against a labeled set."""

    rubric: str
    agreement: float
    correct: int
    total: int
    per_example: Dict[str, bool] = field(default_factory=dict)


def score_rubric(
    rubric: str,
    labeled: Sequence[Any],
    labels: Sequence[bool],
    judge: RubricJudge,
    *,
    example_keys: Optional[Sequence[str]] = None,
) -> RubricScore:
    """Score a candidate rubric by agreement with a labeled set.

    ``labeled`` and ``labels`` are parallel sequences: ``labels[i]`` is the
    ground-truth verdict for ``labeled[i]``.  Agreement is the fraction of
    examples where ``judge(rubric, example)`` matches the label.  A judge
    exception is treated as a mismatch (never a crash).
    """
    if len(labeled) != len(labels):
        raise ValueError("labeled and labels must be parallel sequences")
    total = len(labeled)
    if total == 0:
        return RubricScore(rubric=rubric, agreement=0.0, correct=0, total=0)

    correct = 0
    per_example: Dict[str, bool] = {}
    for pos, (example, label) in enumerate(zip(labeled, labels)):
        key = example_keys[pos] if example_keys and pos < len(example_keys) else str(pos)
        try:
            verdict = bool(judge(rubric, example))
        except Exception as exc:  # noqa: BLE001 - a judge that throws is a mismatch
            verdict = False
            logger.debug("rubric judge failed on %r: %s", key, exc)
        match = verdict == bool(label)
        per_example[key] = match
        correct += int(match)

    return RubricScore(
        rubric=rubric,
        agreement=correct / total,
        correct=correct,
        total=total,
        per_example=per_example,
    )


def select_best_rubric(
    candidates: Sequence[str],
    labeled: Sequence[Any],
    labels: Sequence[bool],
    judge: RubricJudge,
    *,
    example_keys: Optional[Sequence[str]] = None,
) -> RubricScore:
    """Return the candidate rubric with the highest agreement.

    Ties are broken by the first candidate (stable).  An empty candidate
    list returns a zero-agreement score with an empty rubric.
    """
    if not candidates:
        return RubricScore(rubric="", agreement=0.0, correct=0, total=len(labeled))
    best: Optional[RubricScore] = None
    for candidate in candidates:
        score = score_rubric(
            candidate, labeled, labels, judge, example_keys=example_keys
        )
        if best is None or score.agreement > best.agreement:
            best = score
    assert best is not None
    return best
