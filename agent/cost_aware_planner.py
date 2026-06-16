"""Cost-aware multi-tool orchestration planner (first increment of #253).

This is the planning/estimation core for cost-aware multi-tool orchestration.
Given a set of candidate tool steps — each declaring its dependencies and an
estimated token footprint, optionally across several candidate model routes —
it produces an executable *topology*:

    * a validated dependency DAG (cycle detection),
    * topological "waves" of mutually-independent steps that are safe to run
      in parallel,
    * a per-step recommended model route (the cheapest route with known
      pricing), and
    * an aggregate cost estimate that distinguishes the critical-path serial
      cost from the wave-parallel cost.

It builds directly on the existing pricing engine in ``agent.usage_pricing``
(``CanonicalUsage`` / ``estimate_usage_cost`` / ``CostResult``) rather than
re-deriving any pricing. No tokens are spent here: every cost is an estimate
from the caller-provided token footprint.

Deep dispatch wiring (actually executing the waves via ``delegate_task`` /
async tool calls, and re-planning on failure) is intentionally deferred to a
later increment — see issue #253. This module is the deterministic, fully
testable foundation those steps will consume.

Example
-------
    from agent.cost_aware_planner import (
        PlanStep, ToolRoute, TokenFootprint, plan_orchestration,
    )

    steps = [
        PlanStep(
            id="search_docs",
            footprint=TokenFootprint(input_tokens=2000, output_tokens=800),
            routes=[
                ToolRoute(model="claude-haiku-4-5", provider="anthropic"),
                ToolRoute(model="claude-opus-4-8", provider="anthropic"),
            ],
        ),
        PlanStep(
            id="search_web",
            footprint=TokenFootprint(input_tokens=1500, output_tokens=600),
        ),
        PlanStep(
            id="synthesize",
            depends_on=("search_docs", "search_web"),
            footprint=TokenFootprint(input_tokens=6000, output_tokens=2000),
        ),
    ]
    plan = plan_orchestration(steps)
    # plan.waves -> [["search_docs", "search_web"], ["synthesize"]]
    # plan.total_estimated_cost_usd -> Decimal sum across steps
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from agent.usage_pricing import (
    CanonicalUsage,
    CostStatus,
    estimate_usage_cost,
)

_ZERO = Decimal("0")


class PlanValidationError(ValueError):
    """Raised when a set of plan steps cannot form a valid DAG.

    Covers duplicate step ids, dangling dependencies (a step depends on an id
    that is not present), and dependency cycles.
    """


@dataclass(frozen=True)
class TokenFootprint:
    """Estimated token footprint for a single tool step.

    Mirrors the buckets the pricing engine prices. All values are estimates
    supplied by the caller (e.g. from a heuristic or a prior run); the planner
    never invents token counts.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def to_usage(self) -> CanonicalUsage:
        return CanonicalUsage(
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cache_read_tokens=self.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens,
        )


@dataclass(frozen=True)
class ToolRoute:
    """A candidate model/provider route a step could be executed on.

    A step with several routes lets the planner pick the cheapest route that
    has known pricing. ``provider`` / ``base_url`` are passed straight through
    to the pricing engine's billing-route resolution.
    """

    model: str
    provider: Optional[str] = None
    base_url: Optional[str] = None


@dataclass(frozen=True)
class PlanStep:
    """A single tool invocation in the orchestration plan.

    ``depends_on`` lists the ids of steps whose output this step consumes;
    those edges define the DAG. A step with no routes is treated as having a
    single empty-model route, which prices as "unknown" — useful for
    non-LLM tools (a pure HTTP fetch, a file read) that still participate in
    the topology but carry no model cost.
    """

    id: str
    footprint: TokenFootprint = field(default_factory=TokenFootprint)
    routes: tuple[ToolRoute, ...] = ()
    depends_on: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # Normalize routes/depends_on to tuples so callers may pass lists.
        object.__setattr__(self, "routes", tuple(self.routes))
        object.__setattr__(self, "depends_on", tuple(self.depends_on))


@dataclass(frozen=True)
class StepCostEstimate:
    """Costed result for one step after route selection."""

    step_id: str
    recommended_route: Optional[ToolRoute]
    estimated_cost_usd: Optional[Decimal]
    cost_status: CostStatus
    # All routes evaluated, cheapest first, as (route, cost-or-None) pairs.
    evaluated_routes: tuple[tuple[ToolRoute, Optional[Decimal]], ...] = ()


@dataclass(frozen=True)
class OrchestrationPlan:
    """The recommended topology and cost breakdown for a set of steps."""

    # Topological waves: each inner list is a set of step ids with no
    # dependency between them, safe to dispatch in parallel. Waves run in
    # order; every step in wave N only depends on steps in waves < N.
    waves: tuple[tuple[str, ...], ...]
    # A valid serial ordering (flattened waves) for callers that cannot run
    # steps in parallel.
    serial_order: tuple[str, ...]
    step_estimates: dict[str, StepCostEstimate]
    # Sum of every step's recommended cost (steps with unknown cost are
    # excluded from the sum but flagged in ``unknown_cost_steps``).
    total_estimated_cost_usd: Decimal
    # Cost of the most expensive step in each wave, summed — an estimate of
    # the spend on the critical path when waves run fully in parallel.
    critical_path_cost_usd: Decimal
    unknown_cost_steps: tuple[str, ...]

    @property
    def max_parallelism(self) -> int:
        """Largest number of steps in any single wave."""
        return max((len(w) for w in self.waves), default=0)


def _cheapest_route(step: PlanStep) -> StepCostEstimate:
    """Evaluate every candidate route for a step and pick the cheapest priced.

    Routes whose pricing is unknown are kept (with a ``None`` cost) but never
    chosen over a route with a known cost. If no route has known pricing the
    step's cost is reported as unknown and the first route — if any — is the
    recommendation so the caller still has something dispatchable.
    """
    usage = step.footprint.to_usage()
    routes = step.routes or (ToolRoute(model=""),)

    evaluated: list[tuple[ToolRoute, Optional[Decimal]]] = []
    for route in routes:
        result = estimate_usage_cost(
            route.model,
            usage,
            provider=route.provider,
            base_url=route.base_url,
        )
        cost = result.amount_usd if result.status in ("actual", "estimated", "included") else None
        evaluated.append((route, cost))

    # Sort: priced routes first (cheapest cost ascending), unknown routes last
    # but in their original order (stable sort preserves it).
    def _sort_key(item: tuple[ToolRoute, Optional[Decimal]]) -> tuple[int, Decimal]:
        _, cost = item
        if cost is None:
            return (1, _ZERO)
        return (0, cost)

    evaluated.sort(key=_sort_key)

    best_route, best_cost = evaluated[0]
    if best_cost is None:
        return StepCostEstimate(
            step_id=step.id,
            recommended_route=best_route if step.routes else None,
            estimated_cost_usd=None,
            cost_status="unknown",
            evaluated_routes=tuple(evaluated),
        )
    return StepCostEstimate(
        step_id=step.id,
        recommended_route=best_route,
        estimated_cost_usd=best_cost,
        cost_status="estimated",
        evaluated_routes=tuple(evaluated),
    )


def _topological_waves(steps: list[PlanStep]) -> tuple[tuple[str, ...], ...]:
    """Group steps into dependency waves via Kahn's algorithm by level.

    Each wave is the set of currently-ready steps (in-degree 0). Within a wave
    step ids are sorted for deterministic output. Raises
    ``PlanValidationError`` on duplicate ids, dangling dependencies, or cycles.
    """
    by_id: dict[str, PlanStep] = {}
    for step in steps:
        if step.id in by_id:
            raise PlanValidationError(f"duplicate step id: {step.id!r}")
        by_id[step.id] = step

    # Validate dependency targets exist before building the graph.
    for step in steps:
        for dep in step.depends_on:
            if dep not in by_id:
                raise PlanValidationError(
                    f"step {step.id!r} depends on unknown step {dep!r}"
                )
            if dep == step.id:
                raise PlanValidationError(f"step {step.id!r} depends on itself")

    indegree: dict[str, int] = {sid: 0 for sid in by_id}
    dependents: dict[str, list[str]] = {sid: [] for sid in by_id}
    for step in steps:
        # De-dupe a step's own dependency list so a repeated edge does not
        # inflate the in-degree and strand the step.
        for dep in set(step.depends_on):
            indegree[step.id] += 1
            dependents[dep].append(step.id)

    waves: list[tuple[str, ...]] = []
    ready = sorted(sid for sid, deg in indegree.items() if deg == 0)
    resolved = 0
    while ready:
        waves.append(tuple(ready))
        next_ready: list[str] = []
        for sid in ready:
            resolved += 1
            for dependent in dependents[sid]:
                indegree[dependent] -= 1
                if indegree[dependent] == 0:
                    next_ready.append(dependent)
        ready = sorted(next_ready)

    if resolved != len(by_id):
        stuck = sorted(sid for sid, deg in indegree.items() if deg > 0)
        raise PlanValidationError(
            f"dependency cycle detected among steps: {stuck}"
        )

    return tuple(waves)


def plan_orchestration(steps: list[PlanStep]) -> OrchestrationPlan:
    """Build a cost-aware orchestration plan from a list of steps.

    Returns an :class:`OrchestrationPlan` with the topological waves, a valid
    serial ordering, per-step cost estimates (cheapest priced route chosen),
    and aggregate cost figures. Raises :class:`PlanValidationError` if the
    steps do not form a valid DAG.

    The aggregate ``total_estimated_cost_usd`` sums every step's recommended
    cost (the spend if all steps run). ``critical_path_cost_usd`` sums the most
    expensive step in each wave — the spend bounded by the longest dependency
    chain when independent waves overlap in time. Both exclude steps whose cost
    is unknown; those ids are listed in ``unknown_cost_steps``.
    """
    if not steps:
        return OrchestrationPlan(
            waves=(),
            serial_order=(),
            step_estimates={},
            total_estimated_cost_usd=_ZERO,
            critical_path_cost_usd=_ZERO,
            unknown_cost_steps=(),
        )

    waves = _topological_waves(steps)

    estimates: dict[str, StepCostEstimate] = {}
    for step in steps:
        estimates[step.id] = _cheapest_route(step)

    total = _ZERO
    unknown: list[str] = []
    for sid, est in estimates.items():
        if est.estimated_cost_usd is None:
            unknown.append(sid)
        else:
            total += est.estimated_cost_usd

    critical_path = _ZERO
    for wave in waves:
        wave_costs = [
            estimates[sid].estimated_cost_usd
            for sid in wave
            if estimates[sid].estimated_cost_usd is not None
        ]
        if wave_costs:
            critical_path += max(wave_costs)

    serial_order = tuple(sid for wave in waves for sid in wave)

    return OrchestrationPlan(
        waves=waves,
        serial_order=serial_order,
        step_estimates=estimates,
        total_estimated_cost_usd=total,
        critical_path_cost_usd=critical_path,
        unknown_cost_steps=tuple(sorted(unknown)),
    )


def format_plan_summary(plan: OrchestrationPlan) -> str:
    """Render a compact, human-readable summary of an orchestration plan.

    Intended for logs / CLI diagnostics. Pure formatting — does not recompute
    anything.
    """
    if not plan.waves:
        return "empty plan (no steps)"

    lines: list[str] = []
    lines.append(
        f"Plan: {len(plan.serial_order)} steps in {len(plan.waves)} waves "
        f"(max parallelism {plan.max_parallelism})"
    )
    for i, wave in enumerate(plan.waves):
        parts = []
        for sid in wave:
            est = plan.step_estimates[sid]
            if est.estimated_cost_usd is None:
                parts.append(f"{sid} (~$?)")
            else:
                model = est.recommended_route.model if est.recommended_route else ""
                tag = f" via {model}" if model else ""
                parts.append(f"{sid} (~${est.estimated_cost_usd:.4f}{tag})")
        lines.append(f"  wave {i + 1}: " + ", ".join(parts))

    lines.append(f"Total estimated cost: ~${plan.total_estimated_cost_usd:.4f}")
    lines.append(f"Critical-path cost:   ~${plan.critical_path_cost_usd:.4f}")
    if plan.unknown_cost_steps:
        lines.append(
            "Unknown-cost steps: " + ", ".join(plan.unknown_cost_steps)
        )
    return "\n".join(lines)
