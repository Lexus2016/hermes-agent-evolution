#!/usr/bin/env python3
"""GEPA reflector: textual critique generation from skill-variant trajectories (issue #2231, Slice A).

Implements the **reflect** step of GEPA (Generative Evolutionary Program
Assembly): after evaluating N candidate skill variants against a task set, an
LLM analyzes the successes and failures to produce natural-language critiques
("why"), not just scalar scores.

This is a **standalone module** — no changes to the existing evaluation path.
It takes evaluation results as input and produces structured textual critiques
as output, stored for later use by the mutation step (Slice B).

Input contract (evaluation results):
    A list of variant results, each with:
      - ``variant``: the skill variant name/id
      - ``task``: the task id/description
      - ``passed``: bool (pass/fail)
      - ``task_data``: optional dict of task-specific data

Output contract (critiques):
    A list of per-variant, per-task critique objects:
      - ``variant``, ``task``, ``passed``
      - ``critique``: natural-language explanation of why it succeeded/failed
      - ``signals``: structured list of extracted success/failure signals

The critique generation is deterministic (rule-based) by default so it is
unit-testable and safe to run without an LLM. An optional LLM callback can be
supplied to produce richer natural-language critiques.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class VariantResult:
    """A single skill-variant evaluation result."""

    variant: str
    task: str
    passed: bool
    task_data: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Critique:
    """A textual critique for one variant on one task."""

    variant: str
    task: str
    passed: bool
    critique: str
    signals: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "variant": self.variant,
            "task": self.task,
            "passed": self.passed,
            "critique": self.critique,
            "signals": self.signals,
        }


# ── Deterministic critique generation ────────────────────────────────────

def _default_critique(result: VariantResult) -> Critique:
    """Generate a deterministic, rule-based critique for a single result.

    Produces a natural-language "why" from the pass/fail signal plus any
    structured task data. This is the no-LLM fallback — it is deterministic
    and unit-testable.
    """
    variant = result.variant
    task = result.task
    signals: List[str] = []

    if result.passed:
        signals.append("success")
        if result.task_data:
            signals.append("task_data_present")
        critique = (
            f"Variant '{variant}' succeeded on task '{task}'. "
            "The procedure produced the expected outcome; this success "
            "signal can be reinforced in the next generation."
        )
    else:
        signals.append("failure")
        if result.task_data:
            signals.append("task_data_present")
        critique = (
            f"Variant '{variant}' failed on task '{task}'. "
            "The procedure did not produce the expected outcome; this "
            "failure signal should be analyzed and corrected in the next "
            "generation."
        )

    return Critique(
        variant=variant,
        task=task,
        passed=result.passed,
        critique=critique,
        signals=signals,
    )


# ── LLM-backed critique generation (optional) ────────────────────────────

LLMCallback = Callable[[List[VariantResult]], List[Critique]]


def _llm_critiques(
    results: List[VariantResult],
    llm: LLMCallback,
) -> List[Critique]:
    """Generate critiques via an LLM callback.

    The callback receives the full result set and must return a list of
    Critique objects (one per input result, in the same order). If the
    callback raises or returns a mismatched list, we fall back to the
    deterministic generator so the reflector never hard-fails.
    """
    try:
        critiques = llm(results)
        if len(critiques) != len(results):
            logger.warning(
                "reflector: LLM returned %d critiques for %d results — "
                "falling back to deterministic",
                len(critiques),
                len(results),
            )
            return [_default_critique(r) for r in results]
        return critiques
    except Exception as exc:
        logger.warning("reflector: LLM critique generation failed (%s) — falling back", exc)
        return [_default_critique(r) for r in results]


# ── Public API ───────────────────────────────────────────────────────────

def reflect(
    results: List[VariantResult],
    llm: Optional[LLMCallback] = None,
) -> List[Critique]:
    """Produce textual critiques for a set of skill-variant evaluation results.

    Args:
        results: the evaluation results (pass/fail + task data) for N variants.
        llm: optional LLM callback for richer natural-language critiques. If
            omitted (or if it fails), deterministic rule-based critiques are
            produced.

    Returns:
        A list of Critique objects, one per input result, in the same order.
    """
    if llm is not None:
        return _llm_critiques(results, llm)
    return [_default_critique(r) for r in results]


def reflect_to_json(
    results: List[VariantResult],
    llm: Optional[LLMCallback] = None,
) -> str:
    """Reflect and serialize the critiques to JSON (for storage)."""
    critiques = reflect(results, llm=llm)
    return json.dumps([c.to_dict() for c in critiques], indent=2)


def store_critiques(
    critiques: List[Critique],
    path: str,
) -> None:
    """Store critiques alongside the evaluation results (JSONL append).

    Each critique is written as one JSON line so the mutation step (Slice B)
    can read them back later.
    """
    import os

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        for c in critiques:
            fh.write(json.dumps(c.to_dict()) + "\n")