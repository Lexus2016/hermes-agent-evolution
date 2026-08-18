# -*- coding: utf-8 -*-
"""RubricForge — agreement-maximization primitive for rubric evolution (#2780).

Child of #2760. A rubric is only as good as its agreement with a labeled set
of known-good / known-bad examples. This module scores a candidate rubric
against a labeled set (fraction of examples whose judge verdict matches the
label) and selects the agreement-maximizing candidate. The rubric judge
applies the winner at scoring time (``evolution_rubric_judge.resolve_active_rubric``)
— the primitive is consumed by the real eval pipeline, not importable-only.

The judge is injectable (``judge(rubric_text, example) -> bool``) so the
agreement computation is deterministic and LLM-free; a judge exception counts
as a mismatch, never a crash.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, List, Sequence, Tuple

logger = logging.getLogger(__name__)

# Applies a rubric text to one labeled example; True = the rubric accepts it.
RubricJudge = Callable[[str, Any], bool]


def score_rubric(
    rubric: str,
    labeled: Sequence[Any],
    labels: Sequence[bool],
    judge: RubricJudge,
) -> Tuple[float, List[bool]]:
    """Agreement of one candidate rubric against a labeled set.

    Returns ``(agreement, per_example)`` where ``per_example[i]`` is whether
    the judge verdict matched ``labels[i]``. Zero-division safe: an empty
    labeled set scores 0.0.
    """
    if len(labeled) != len(labels):
        raise ValueError("labeled and labels must be parallel sequences")
    per_example: List[bool] = []
    for example, label in zip(labeled, labels):
        try:
            verdict = bool(judge(rubric, example))
        except Exception as exc:  # noqa: BLE001 - a throwing judge is a mismatch
            verdict = False
            logger.debug("rubric judge failed on %r: %s", example, exc)
        per_example.append(verdict == bool(label))
    agreement = sum(per_example) / len(per_example) if per_example else 0.0
    return agreement, per_example


def select_best_rubric(
    candidates: Sequence[str],
    labeled: Sequence[Any],
    labels: Sequence[bool],
    judge: RubricJudge,
) -> Tuple[str, float]:
    """Return ``(best_rubric_text, agreement)`` — highest agreement wins.

    Ties break toward the FIRST candidate (stable), so candidate ordering is
    the documented tie-break policy. An empty candidate list returns
    ``("", 0.0)`` — callers treat an empty winner as "no selection".
    """
    best_text, best_agreement = "", 0.0
    for candidate in candidates:
        agreement, _ = score_rubric(candidate, labeled, labels, judge)
        if agreement > best_agreement:
            best_text, best_agreement = candidate, agreement
    return best_text, best_agreement
