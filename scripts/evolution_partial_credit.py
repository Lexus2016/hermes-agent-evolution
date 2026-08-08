#!/usr/bin/env python3
"""Continuous partial-credit replay grader (#1302).

Replaces binary pass/fail grading with continuous partial-credit bands (0.0 to 1.0)
to expose long-horizon progress and 'dies-still-working' failure modes (LHTB / Senior-SWE-Bench).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass(frozen=True)
class PartialCreditGrade:
    score: float  # 0.0 to 1.0
    solved: bool  # True if score >= 0.95
    band: str     # "unsolved_low" (0-0.25), "unsolved_mid" (0.25-0.70), "unsolved_high" (0.70-0.95), "solved" (>=0.95)
    progress_made: bool  # True if score > 0.0 but not solved (dies-still-working candidate)


def grade_partial_credit(
    tests_passed: int, total_tests: int, step_completion_ratio: float = 1.0
) -> PartialCreditGrade:
    """Compute continuous partial-credit score and band classification."""
    if total_tests <= 0:
        raw_score = min(1.0, max(0.0, step_completion_ratio))
    else:
        test_ratio = min(1.0, max(0.0, tests_passed / total_tests))
        raw_score = min(1.0, max(0.0, test_ratio * step_completion_ratio))

    score = round(raw_score, 4)
    solved = score >= 0.95

    if solved:
        band = "solved"
    elif score >= 0.70:
        band = "unsolved_high"
    elif score >= 0.25:
        band = "unsolved_mid"
    else:
        band = "unsolved_low"

    progress_made = 0.0 < score < 0.95

    return PartialCreditGrade(
        score=score,
        solved=solved,
        band=band,
        progress_made=progress_made,
    )
