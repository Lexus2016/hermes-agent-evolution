# -*- coding: utf-8 -*-
"""Parallel batch tool evolution + cross-domain transfer gate (#2260).

Slice B of the In-Situ Self-Evolving paradigm (parent #2248; Slice A is
the tool synthesis harness in ``tool_synthesis.py``, #2259): evolve a
tool by proposing mutation variants, validating the whole batch in
parallel, keeping the best variant, and accepting it only if it
transfers to held-out domains — not just the origin domain.

Components:

1. **Variant proposer** — generate N mutation variants of a base tool.
   The default is a deterministic string-level mutation of the tool's
   description; an LLM/subagent-backed proposer can be injected without
   changing the harness contract.
2. **Batch runner** — validate all variants in parallel via a thread
   pool; the validator is injectable (production: ``SandboxValidator``).
3. **Selection** — the highest-scoring passing variant wins.
4. **Transferability gate** — an accepted tool must pass on held-out
   domains at ≥ threshold of its origin pass-rate, preventing
   task-specific overfitting (mirrors the validation-overfitting guard
   philosophy from #2480).

New module, no changes to existing tool loading. Module ≤ 200 lines.
"""

from __future__ import annotations

import logging
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence

from evolution.lib.tool_synthesis import SynthesizedTool

logger = logging.getLogger(__name__)

__all__ = [
    "VariantResult",
    "propose_variants",
    "run_batch",
    "select_best",
    "transferability_score",
    "accept_best",
    "variant_distinctness",
    "select_with_anti_conformity",
]

# Deterministic mutation hints cycled across variants; the variant index
# is embedded in each description so variants stay pairwise distinct.
_MUTATION_HINTS = (
    "handle edge cases explicitly",
    "return structured typed results",
    "fail fast on invalid input",
    "keep outputs deterministic",
)


@dataclass
class VariantResult:
    """Outcome of validating one variant."""

    variant: SynthesizedTool
    passed: bool
    score: float


def _mutate_description(base: SynthesizedTool, index: int) -> SynthesizedTool:
    """Default deterministic mutation: rewrite the description field."""
    hint = _MUTATION_HINTS[index % len(_MUTATION_HINTS)]
    description = f"{base.description} [variant {index}: {hint}]"
    code = (
        base.code.replace(base.description, description)
        if base.description and base.description in base.code
        else base.code
    )
    return SynthesizedTool(name=base.name, description=description, code=code)


def propose_variants(
    base_tool: SynthesizedTool,
    n_variants: int,
    proposer: Optional[Callable[[SynthesizedTool, int], SynthesizedTool]] = None,
) -> List[SynthesizedTool]:
    """Propose *n_variants* mutation variants of *base_tool*.

    *proposer* is a callable ``(base_tool, index) -> SynthesizedTool``;
    the default is a deterministic description-level mutation.
    """
    mutate = proposer or _mutate_description
    return [mutate(base_tool, i) for i in range(n_variants)]


def run_batch(
    variants: Sequence[SynthesizedTool],
    validator: Callable[[SynthesizedTool], float],
    max_workers: int = 4,
) -> List[VariantResult]:
    """Validate *variants* in parallel; collect pass/fail + score each.

    *validator* returns a score; bool validators score 1.0/0.0. A
    variant passes when its score is > 0.
    """

    def evaluate(variant: SynthesizedTool) -> VariantResult:
        score = float(validator(variant))
        return VariantResult(variant=variant, passed=score > 0.0, score=score)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        return list(pool.map(evaluate, variants))


def _pass_rate(results: Sequence[VariantResult]) -> float:
    if not results:
        return 0.0
    return sum(1 for r in results if r.passed) / len(results)


def select_best(results: Sequence[VariantResult]) -> Optional[SynthesizedTool]:
    """Return the highest-scoring passing variant, or None if none pass."""
    passing = [r for r in results if r.passed]
    if not passing:
        return None
    return max(passing, key=lambda r: r.score).variant


def transferability_score(
    origin_results: Sequence[VariantResult],
    held_out_results: Sequence[VariantResult],
) -> float:
    """Held-out pass-rate ÷ origin pass-rate (cross-domain transfer).

    1.0 means perfect transfer; below 1.0 degrades on held-out domains.
    Returns 0.0 when the origin pass-rate is 0 (nothing to transfer).
    """
    origin_rate = _pass_rate(origin_results)
    if origin_rate == 0.0:
        return 0.0
    return _pass_rate(held_out_results) / origin_rate


def accept_best(
    origin_results: Sequence[VariantResult],
    held_out_results: Sequence[VariantResult],
    threshold: float = 0.5,
) -> Optional[SynthesizedTool]:
    """Accept the best variant only if it passes origin AND transfers.

    Gate: transferability ≥ *threshold* and the best origin variant
    passed. Prevents overfitting to the origin domain (cf. #2480).
    """
    best = select_best(origin_results)
    if best is None:
        return None
    if transferability_score(origin_results, held_out_results) < threshold:
        return None
    return best


def variant_distinctness(variants: Sequence[SynthesizedTool]) -> float:
    """Diversity metric over a candidate set (anti-conformity, #2761).

    Returns the fraction of variants whose description is pairwise distinct
    from every other variant's description. 1.0 = all variants are unique
    (maximal diversity); 0.0 = every variant is a duplicate of another. A
    population that has converged on one common description scores 0.0 — the
    signal that anti-conformity pressure should kick in.
    """
    if len(variants) < 2:
        return 1.0
    descs = [v.description for v in variants]
    unique = 0
    for i, d in enumerate(descs):
        if all(d != descs[j] for j in range(len(descs)) if j != i):
            unique += 1
    return unique / len(descs)


def select_with_anti_conformity(
    results: Sequence[VariantResult],
    *,
    min_distinctness: float = 0.5,
) -> Optional[SynthesizedTool]:
    """Select the best variant while keeping ≥1 contrarian variant alive.

    Anti-conformity pressure (#2761): once a mediocre variant becomes common
    in shared state, downstream agents conform to it without any selection
    pressure forcing that choice (the conformity law). To resist premature
    convergence, this selection keeps the highest-scoring variant whose
    description is NOT the most common one — a contrarian — when the
    population's distinctness falls below *min_distinctness*.

    Returns the best passing variant, or the best contrarian passing variant
    when the population has converged. None if nothing passes.
    """
    passing = [r for r in results if r.passed]
    if not passing:
        return None
    best = max(passing, key=lambda r: r.score)
    if variant_distinctness([r.variant for r in passing]) >= min_distinctness:
        return best.variant
    # Population has converged — keep a contrarian variant alive.
    descs = [r.variant.description for r in passing]
    counts = Counter(descs)
    most_common = counts.most_common(1)[0][0]
    contrarians = [r for r in passing if r.variant.description != most_common]
    if not contrarians:
        return best.variant
    return max(contrarians, key=lambda r: r.score).variant
