#!/usr/bin/env python3
"""LLMRouter: five-component LLM routing abstraction + xRouteBench evaluation (issue #2317).

Implements the five components of LLM routing from [arXiv:2608.06867]:
  1. Context encoders — transform a request (prompt, tools, history) into a
     routing-relevant context vector.
  2. Model encoders — transform a model's metadata (provider, capabilities,
     cost, latency) into a model representation.
  3. Scoring functions — score a (context, model) pair for compatibility.
  4. Decision rules — select the best model from scored candidates.
  5. Learning signals — record routing outcomes for offline improvement.

Plus **xRouteBench**: an evaluation harness that scores a router against a
set of routing tasks (context → expected model attributes) and computes
routing accuracy. The harness is wired to a multi-provider model registry
interface so it can run against real provider metadata without making
actual LLM calls.

This is a **standalone module** — no changes to the existing agent runtime.
It defines the abstraction so future work can wire it into provider
selection. The scoring and decision logic are deterministic by default,
making the module unit-testable without network access.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)


# ── 1. Context encoders ──────────────────────────────────────────────────

@dataclass
class RoutingContext:
    """Encoded representation of a routing request."""

    prompt_length: int = 0
    has_tools: bool = False
    has_vision: bool = False
    needs_reasoning: bool = False
    estimated_tokens: int = 0
    tags: List[str] = field(default_factory=list)

    def to_vector(self) -> List[float]:
        """Return a fixed-length numeric vector for scoring."""
        return [
            float(self.prompt_length),
            float(self.has_tools),
            float(self.has_vision),
            float(self.needs_reasoning),
            float(self.estimated_tokens),
        ]


class ContextEncoder:
    """Encodes a raw request (dict) into a RoutingContext."""

    def encode(self, request: Dict[str, Any]) -> RoutingContext:
        prompt = str(request.get("prompt") or "")
        tools = request.get("tools") or []
        images = request.get("images") or []
        reasoning = request.get("reasoning") or request.get("needs_reasoning") or False
        return RoutingContext(
            prompt_length=len(prompt),
            has_tools=bool(tools),
            has_vision=bool(images),
            needs_reasoning=bool(reasoning),
            estimated_tokens=int(request.get("estimated_tokens") or len(prompt) // 4),
            tags=list(request.get("tags") or []),
        )


# ── 2. Model encoders ────────────────────────────────────────────────────

@dataclass
class ModelInfo:
    """Metadata for a candidate model (from a multi-provider registry)."""

    name: str
    provider: str
    context_window: int = 0
    supports_tools: bool = False
    supports_vision: bool = False
    supports_reasoning: bool = False
    cost_per_1k: float = 0.0
    latency_ms: int = 0

    def to_vector(self) -> List[float]:
        return [
            float(self.context_window),
            float(self.supports_tools),
            float(self.supports_vision),
            float(self.supports_reasoning),
            self.cost_per_1k,
            float(self.latency_ms),
        ]


class ModelEncoder:
    """Encodes raw model metadata (dict) into a ModelInfo."""

    def encode(self, raw: Dict[str, Any]) -> ModelInfo:
        return ModelInfo(
            name=str(raw.get("name") or raw.get("model") or ""),
            provider=str(raw.get("provider") or ""),
            context_window=int(raw.get("context_window") or raw.get("max_tokens") or 0),
            supports_tools=bool(raw.get("supports_tools") or raw.get("tool_use") or False),
            supports_vision=bool(raw.get("supports_vision") or False),
            supports_reasoning=bool(raw.get("supports_reasoning") or False),
            cost_per_1k=float(raw.get("cost_per_1k") or raw.get("cost") or 0.0),
            latency_ms=int(raw.get("latency_ms") or 0),
        )


# ── 3. Scoring functions ─────────────────────────────────────────────────

class ScoringFunction:
    """Scores a (context, model) pair for routing compatibility.

    The default scorer rewards capability matches and penalizes cost/latency.
    A score > 0 means the model is a viable candidate; higher is better.
    """

    def score(self, ctx: RoutingContext, model: ModelInfo) -> float:
        score = 0.0
        # Capability gating — missing a required capability is a hard penalty.
        if ctx.has_tools and not model.supports_tools:
            score -= 100.0
        if ctx.has_vision and not model.supports_vision:
            score -= 100.0
        if ctx.needs_reasoning and not model.supports_reasoning:
            score -= 50.0
        # Context window must fit the request.
        if model.context_window > 0 and ctx.estimated_tokens > model.context_window:
            score -= 100.0
        # Reward capability matches.
        if ctx.has_tools and model.supports_tools:
            score += 10.0
        if ctx.has_vision and model.supports_vision:
            score += 10.0
        if ctx.needs_reasoning and model.supports_reasoning:
            score += 10.0
        # Penalize cost and latency (normalized).
        score -= model.cost_per_1k * 5.0
        score -= model.latency_ms / 1000.0
        return score


# ── 4. Decision rules ────────────────────────────────────────────────────

class DecisionRule:
    """Selects the best model from scored candidates.

    The default rule is greedy argmax. A ``min_score`` threshold can be set
    to reject all candidates if none meet the bar (returns None).
    """

    def __init__(self, min_score: float = -100.0) -> None:
        self.min_score = min_score

    def decide(self, scores: Dict[str, float]) -> Optional[str]:
        if not scores:
            return None
        best_name = max(scores, key=lambda k: scores[k])
        best_score = scores[best_name]
        if best_score < self.min_score:
            return None
        return best_name


# ── 5. Learning signals ──────────────────────────────────────────────────

@dataclass
class RoutingOutcome:
    """Recorded outcome of a routing decision for offline learning."""

    request: Dict[str, Any]
    selected_model: str
    success: bool
    latency_ms: int = 0
    cost: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "request": self.request,
            "selected_model": self.selected_model,
            "success": self.success,
            "latency_ms": self.latency_ms,
            "cost": self.cost,
        }


class LearningSignalCollector:
    """Collects routing outcomes for offline router improvement."""

    def __init__(self) -> None:
        self._outcomes: List[RoutingOutcome] = []

    def record(self, outcome: RoutingOutcome) -> None:
        self._outcomes.append(outcome)

    def all(self) -> List[RoutingOutcome]:
        return list(self._outcomes)

    def accuracy(self) -> float:
        if not self._outcomes:
            return 0.0
        successes = sum(1 for o in self._outcomes if o.success)
        return successes / len(self._outcomes)

    def to_jsonl(self, path: str) -> None:
        import os
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            for o in self._outcomes:
                fh.write(json.dumps(o.to_dict()) + "\n")


# ── LLMRouter (assembles the five components) ────────────────────────────

class LLMRouter:
    """Assembles context encoder, model encoder, scorer, decision rule, and
    learning-signal collector into a routing pipeline.

    Usage::

        router = LLMRouter(models=[...])
        model = router.route({"prompt": "...", "tools": [...]})
    """

    def __init__(
        self,
        models: Sequence[Dict[str, Any]],
        context_encoder: Optional[ContextEncoder] = None,
        model_encoder: Optional[ModelEncoder] = None,
        scorer: Optional[ScoringFunction] = None,
        decision: Optional[DecisionRule] = None,
        learner: Optional[LearningSignalCollector] = None,
    ) -> None:
        self.context_encoder = context_encoder or ContextEncoder()
        self.model_encoder = model_encoder or ModelEncoder()
        self.scorer = scorer or ScoringFunction()
        self.decision = decision or DecisionRule()
        self.learner = learner or LearningSignalCollector()
        self._models: List[ModelInfo] = [self.model_encoder.encode(m) for m in models]

    def route(self, request: Dict[str, Any]) -> Optional[str]:
        ctx = self.context_encoder.encode(request)
        scores: Dict[str, float] = {}
        for model in self._models:
            scores[model.name] = self.scorer.score(ctx, model)
        return self.decision.decide(scores)

    def route_with_context(self, request: Dict[str, Any]) -> tuple:
        """Route and return (model_name, context, scores) for debugging/bench."""
        ctx = self.context_encoder.encode(request)
        scores: Dict[str, float] = {}
        for model in self._models:
            scores[model.name] = self.scorer.score(ctx, model)
        chosen = self.decision.decide(scores)
        return chosen, ctx, scores


# ── xRouteBench evaluation harness ───────────────────────────────────────

@dataclass
class BenchTask:
    """A single xRouteBench routing task.

    ``request`` is the input to the router; ``expected_attrs`` describes the
    attributes the selected model *must* have (not the exact model name —
    xRouteBench evaluates attribute-level routing accuracy, not memorization).
    """

    request: Dict[str, Any]
    expected_attrs: Dict[str, Any] = field(default_factory=dict)


class xRouteBench:
    """Evaluation harness: scores a router against a set of routing tasks.

    A routing decision is "correct" if the selected model satisfies all
    ``expected_attrs`` for that task. The harness uses the router's own model
    registry to check attributes, so it requires no external API calls.
    """

    def __init__(self, router: LLMRouter, tasks: Optional[List[BenchTask]] = None) -> None:
        self.router = router
        self.tasks: List[BenchTask] = tasks or _default_bench_tasks()

    def evaluate(self) -> Dict[str, Any]:
        total = len(self.tasks)
        correct = 0
        per_task: List[Dict[str, Any]] = []
        model_map = {m.name: m for m in self.router._models}
        for task in self.tasks:
            chosen, ctx, scores = self.router.route_with_context(task.request)
            is_correct = self._check(chosen, task.expected_attrs, model_map)
            if is_correct:
                correct += 1
            per_task.append({
                "chosen": chosen,
                "correct": is_correct,
                "expected": task.expected_attrs,
            })
        accuracy = correct / total if total else 0.0
        return {"accuracy": accuracy, "correct": correct, "total": total, "per_task": per_task}

    def _check(
        self,
        chosen: Optional[str],
        expected: Dict[str, Any],
        model_map: Dict[str, ModelInfo],
    ) -> bool:
        if not chosen or chosen not in model_map:
            return False
        model = model_map[chosen]
        for attr, expected_val in expected.items():
            actual = getattr(model, attr, None)
            if actual != expected_val and bool(actual) != bool(expected_val):
                return False
        return True


def _default_bench_tasks() -> List[BenchTask]:
    """A minimal default task set for smoke-testing the harness."""
    return [
        BenchTask(
            request={"prompt": "x" * 200, "tools": [{"name": "calc"}]},
            expected_attrs={"supports_tools": True},
        ),
        BenchTask(
            request={"prompt": "describe this image", "images": ["data:..."]},
            expected_attrs={"supports_vision": True},
        ),
        BenchTask(
            request={"prompt": "think step by step", "reasoning": True},
            expected_attrs={"supports_reasoning": True},
        ),
        BenchTask(
            request={"prompt": "hello", "estimated_tokens": 10},
            expected_attrs={},  # no hard constraint — any model is fine
        ),
    ]