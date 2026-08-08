"""Selective memory addition gate (#1270).

Prevents noisy/misaligned records from polluting agent memory by scoring candidate utility
before storage (Experience-Following Property, ACL 2026).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict


@dataclass(frozen=True)
class MemoryAdditionDecision:
    allow_addition: bool
    score: float  # 0.0 to 1.0
    reason: str


def evaluate_memory_addition(
    content: str,
    eval_score: float = 1.0,
    min_utility_threshold: float = 0.6,
) -> MemoryAdditionDecision:
    """Evaluate whether a memory candidate passes the quality/utility addition gate."""
    if not content or not content.strip():
        return MemoryAdditionDecision(
            allow_addition=False,
            score=0.0,
            reason="Empty memory content",
        )

    # Base score combines verification score and length/noise heuristics
    text_len = len(content.strip())
    is_noise = text_len < 10 or "error: traceback" in content.lower()
    quality_mult = 0.5 if is_noise else 1.0

    score = round(min(1.0, max(0.0, eval_score * quality_mult)), 4)
    allow = score >= min_utility_threshold

    reason = "Utility score passes threshold" if allow else f"Utility score {score} below threshold {min_utility_threshold}"

    return MemoryAdditionDecision(
        allow_addition=allow,
        score=score,
        reason=reason,
    )
