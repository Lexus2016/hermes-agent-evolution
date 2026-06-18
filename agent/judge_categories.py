"""Category-aware aggregation over Agent-as-a-Judge verdicts (issue #318).

A small, additive layer on top of ``agent.agent_judge``.  Headline judge
success rates are averages, and persistent blind spots (business-logic flaws,
race conditions, vulnerability recognition) are hidden inside them.  This module
takes a batch of already-computed :class:`~agent.agent_judge.JudgeVerdict`s —
each carrying a benchmark-task ``category`` label — and groups them into
per-category pass rates, then surfaces the weakest categories as targets for the
evolution loop's next skill/prompt mutation.

Design (deliberately minimal):

* **Stats** (:class:`CategoryStats`) — an immutable per-category roll-up
  (``n`` scored, ``passed`` count, derived ``pass_rate``) with a JSON-friendly
  :meth:`CategoryStats.to_dict` for the evolution report.
* **Aggregation** (:func:`aggregate_by_category`) — a pure function grouping
  ``(category, verdict)`` pairs into ``{category: CategoryStats}``.  Pass/fail
  uses :meth:`JudgeVerdict.passed` so the threshold semantics are identical to
  every other consumer of a verdict.
* **Targeting** (:func:`weakest_categories`) — a pure function ranking
  categories worst-pass-rate-first; a zero-pass category is always a target.
* **Report** (:func:`build_category_report`) — the JSON-serialisable shape the
  evolution report embeds (``per_category`` + ``weakest_categories``).

SAFETY / ADDITIVITY
-------------------
Nothing here is wired into ``AgentJudge.score``; whole-trace scoring is
byte-for-byte unchanged.  This module only *reads* verdicts a caller already
produced — it never re-scores, calls an LLM, or touches the rubric.  The
``category`` label lives on the caller's benchmark-task record, not inside the
verdict, so labels are stable across re-runs of the same benchmark.

Reference
---------
* Agent-as-a-Judge, ICML 2025 — https://proceedings.mlr.press/v267/zhuge25a.html
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple

from agent.agent_judge import JudgeVerdict

# Default pass/fail threshold, matching ``JudgeVerdict.passed``'s own default so
# category pass rates agree with every other verdict consumer out of the box.
DEFAULT_THRESHOLD = 0.7

# A labelled verdict: the benchmark-task category and the verdict scored for it.
VerdictWithCategory = Tuple[str, JudgeVerdict]


@dataclass(frozen=True)
class CategoryStats:
    """Per-category roll-up of pass/fail outcomes.

    Attributes:
        category: The benchmark-task category label (whitespace-normalised).
        n: Number of verdicts scored in this category.
        passed: How many of those verdicts cleared the pass threshold.
    """

    category: str
    n: int
    passed: int

    @property
    def pass_rate(self) -> float:
        """Fraction of verdicts that passed; 0.0 for an empty category."""
        return self.passed / self.n if self.n else 0.0

    def to_dict(self) -> Dict[str, float]:
        """JSON-serialisable form for the evolution report."""
        return {
            "pass_rate": round(self.pass_rate, 4),
            "n": self.n,
            "passed": self.passed,
        }


def aggregate_by_category(
    verdicts_with_category: Sequence[VerdictWithCategory],
    threshold: float = DEFAULT_THRESHOLD,
) -> Dict[str, CategoryStats]:
    """Group labelled verdicts into per-category pass rates.

    Pure and deterministic — no LLM, no I/O.  Each ``(category, verdict)`` pair
    contributes one trial to its category; the verdict passes when
    ``verdict.passed(threshold)`` is true (identical >= semantics to every other
    verdict consumer).  Category labels are whitespace-normalised so a stray
    space does not fork a category across benchmark re-runs.

    Args:
        verdicts_with_category: ``(category, verdict)`` pairs to aggregate.
        threshold: Pass/fail cutoff handed to :meth:`JudgeVerdict.passed`.

    Returns:
        ``{category: CategoryStats}`` for every category seen (empty if the
        batch is empty).

    Raises:
        ValueError: if a category label is empty after whitespace stripping.
    """
    counts: Dict[str, List[int]] = {}  # category -> [n, passed]
    for category, verdict in verdicts_with_category:
        label = category.strip()
        if not label:
            raise ValueError("category label must be a non-empty string")
        bucket = counts.setdefault(label, [0, 0])
        bucket[0] += 1
        if verdict.passed(threshold):
            bucket[1] += 1
    return {
        label: CategoryStats(category=label, n=n, passed=passed)
        for label, (n, passed) in counts.items()
    }


def weakest_categories(
    aggregated: Dict[str, CategoryStats],
    *,
    max_pass_rate: float = 1.0,
    limit: int = 0,
) -> List[CategoryStats]:
    """Rank categories worst-pass-rate-first as evolution targets.

    Pure and deterministic.  Ties on pass rate break by category name so the
    ordering — and therefore the chosen mutation target — is stable across
    re-runs of the same benchmark.

    Args:
        aggregated: Output of :func:`aggregate_by_category`.
        max_pass_rate: Keep only categories at or below this pass rate (default
            1.0 keeps all).  ``0.0`` surfaces only zero-pass categories.
        limit: Cap the number of targets returned; ``0`` (default) means no cap.

    Returns:
        :class:`CategoryStats` ordered weakest-first, filtered and capped.
    """
    targets = [
        stats
        for stats in aggregated.values()
        if stats.pass_rate <= max_pass_rate
    ]
    targets.sort(key=lambda s: (s.pass_rate, s.category))
    if limit > 0:
        targets = targets[:limit]
    return targets


def build_category_report(
    verdicts_with_category: Sequence[VerdictWithCategory],
    threshold: float = DEFAULT_THRESHOLD,
    *,
    max_pass_rate: float = 0.5,
    limit: int = 0,
) -> Dict[str, object]:
    """Build the JSON-serialisable category breakdown for the evolution report.

    Combines :func:`aggregate_by_category` and :func:`weakest_categories` into
    the shape the evolution report embeds: a full per-category breakdown plus an
    ordered list of weakest-category labels (the mutation targets).

    Args:
        verdicts_with_category: ``(category, verdict)`` pairs to report on.
        threshold: Pass/fail cutoff for aggregation.
        max_pass_rate: Weakest-category cutoff (default 0.5 — categories passing
            at most half the time are targets; a zero-pass category always
            qualifies).
        limit: Cap on the number of weakest-category targets (``0`` = no cap).

    Returns:
        ``{"per_category": {cat: stats_dict}, "weakest_categories": [cat, ...]}``.
    """
    aggregated = aggregate_by_category(verdicts_with_category, threshold)
    weakest = weakest_categories(
        aggregated, max_pass_rate=max_pass_rate, limit=limit
    )
    return {
        "per_category": {
            cat: stats.to_dict() for cat, stats in aggregated.items()
        },
        "weakest_categories": [stats.category for stats in weakest],
    }
