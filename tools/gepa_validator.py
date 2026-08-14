#!/usr/bin/env python3
"""GEPA held-out validation gate (issue #2232, Slice C).

Validates mutated candidates against an unseen held-out task set before
promotion, preventing overfit to training-set critiques. Promoted
candidates are persisted to a JSONL ledger so downstream consumers
(Slice D skill-system wiring) can read them — nothing stays in memory.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List

from tools.gepa_evolution import Candidate
from tools.gepa_reflector import VariantResult


def default_ledger_path() -> str:
    """Ledger location under HERMES_HOME (override: GEPA_LEDGER_PATH)."""
    override = os.environ.get("GEPA_LEDGER_PATH")
    if override:
        return override
    return os.path.join(
        os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")),
        "evolution",
        "gepa",
        "promotions.jsonl",
    )


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


def persist_promotion(candidate: Candidate, result: HeldOutResult) -> str | None:
    """Append a promotion record to the JSONL ledger; return the path.

    Only promoted (passed) candidates are persisted — the ledger is the
    hand-off point to Slice D consumers. IO errors return ``None``
    rather than raising: validation must never fail because the ledger
    is unwritable.
    """
    if not result.passed:
        return None
    record = {
        "candidate_id": candidate.id,
        "parent_id": candidate.parent_id,
        "generation": candidate.generation,
        "origin": candidate.origin,
        "text": candidate.text,
        "critique_summary": candidate.critique_summary,
        "held_out_validation": {
            "pass_rate": round(result.pass_rate, 4),
            "threshold": result.threshold,
            "train_pass_rate": round(result.train_pass_rate, 4),
            "n_held_out": result.n_held_out,
            "n_passed": result.n_passed,
        },
        "promoted_at": datetime.now(timezone.utc).isoformat(),
    }
    path = default_ledger_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        return None
    return path


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
    if result.passed:
        candidate.metadata["ledger_path"] = persist_promotion(candidate, result)
    return result.passed
