#!/usr/bin/env python3
"""GSME (Gated Semantic Quality-Diversity) Significance & Sealed-Test Gate (#1497).

Implements the three GSME statistical gates (arXiv:2607.13683):
1. Activation gate — verifies the proposed change trigger condition fired during eval.
2. Paired 2-sigma significance test — z >= 1.96 from per-task paired score differences.
3. Sealed-test retention reporting — calculates sealed_lift / training_lift.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


def calculate_paired_z_score(
    baseline_scores: List[float], candidate_scores: List[float]
) -> Tuple[float, bool]:
    """Calculate the paired z-score for per-task differences.

    Returns (z_score, is_significant) where is_significant is True if z >= 1.96.
    """
    if len(baseline_scores) != len(candidate_scores) or len(baseline_scores) < 2:
        return 0.0, False

    diffs = [c - b for b, c in zip(baseline_scores, candidate_scores)]
    n = len(diffs)
    mean_diff = sum(diffs) / n

    variance = sum((d - mean_diff) ** 2 for d in diffs) / (n - 1)
    if variance <= 1e-12:
        # Zero variance: if mean_diff > 0 with >= 2 tasks, consistent improvement
        return (3.0, True) if mean_diff > 0 else (0.0, False)

    std_error = math.sqrt(variance / n)
    z = mean_diff / std_error
    return z, z >= 1.96


def check_activation_gate(trigger_condition: str, execution_log: str) -> bool:
    """Verify that the proposed change's trigger condition fired during execution."""
    if not trigger_condition or not trigger_condition.strip():
        return True  # Always active if no specific trigger condition is specified
    return trigger_condition.strip().lower() in execution_log.lower()


def calculate_retention_rate(
    training_lift: float, sealed_lift: float
) -> Dict[str, float]:
    """Calculate sealed-test retention rate (sealed_lift / training_lift)."""
    if abs(training_lift) < 1e-9:
        retention = 1.0 if abs(sealed_lift) < 1e-9 else 0.0
    else:
        retention = sealed_lift / training_lift

    is_phantom_win = (training_lift > 0 and sealed_lift <= 0) or (retention < 0.5 and training_lift > 0)
    return {
        "training_lift": float(training_lift),
        "sealed_lift": float(sealed_lift),
        "retention_rate": float(retention),
        "is_phantom_win": float(1.0 if is_phantom_win else 0.0),
    }
