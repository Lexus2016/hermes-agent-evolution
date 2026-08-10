#!/usr/bin/env python3
"""Per-task-dimension model performance record + routing table (issue #2257, Slice A).

Maintains a per-task-dimension performance record for each model and routes
each task to the model that has historically performed best on that task type,
with epsilon-greedy exploration to discover better options.

This is a **standalone module** — no changes to existing model selection. The
routing table is a pure data structure: callers record execution outcomes via
``record_outcome()`` and select a model via ``select_model()``.

Task dimensions (coarse, deterministic classification):
  - coding
  - reasoning
  - creative
  - tool-use
  - general (fallback)

Exploration: epsilon-greedy. With probability ``epsilon`` a random model is
chosen (to discover better options and prevent starvation); otherwise the
model with the best historical success rate on the task dimension is chosen.
"""

from __future__ import annotations

import json
import logging
import random
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Canonical task dimensions.
TASK_DIMENSIONS = ("coding", "reasoning", "creative", "tool-use", "general")

# Default exploration rate (epsilon-greedy).
DEFAULT_EPSILON = 0.1


def classify_task(task: Dict[str, Any]) -> str:
    """Classify a task into a coarse dimension.

    Args:
        task: a dict describing the task. Recognized keys:
            - ``dimension``: explicit dimension (used verbatim if valid).
            - ``type``: a free-form type string, matched heuristically.
            - ``tags``: a list of tags, matched heuristically.

    Returns:
        One of TASK_DIMENSIONS. Falls back to ``general``.
    """
    explicit = task.get("dimension")
    if explicit in TASK_DIMENSIONS:
        return explicit

    # Heuristic matching over type + tags.
    haystack = " ".join(
        [
            str(task.get("type", "")),
            " ".join(str(t) for t in task.get("tags", [])),
        ]
    ).lower()

    if any(k in haystack for k in ("code", "coding", "program", "python", "bug")):
        return "coding"
    if any(k in haystack for k in ("reason", "logic", "math", "proof", "deduc")):
        return "reasoning"
    if any(k in haystack for k in ("creat", "write", "story", "poem", "design")):
        return "creative"
    if any(k in haystack for k in ("tool", "shell", "terminal", "api", "call")):
        return "tool-use"
    return "general"


@dataclass
class ModelPerformance:
    """Per-model, per-dimension performance record."""

    model: str
    dimension: str
    attempts: int = 0
    successes: int = 0

    @property
    def success_rate(self) -> Optional[float]:
        if self.attempts == 0:
            return None
        return self.successes / self.attempts

    def record(self, success: bool) -> None:
        self.attempts += 1
        if success:
            self.successes += 1

    def to_dict(self) -> Dict[str, Any]:
        return {
            "model": self.model,
            "dimension": self.dimension,
            "attempts": self.attempts,
            "successes": self.successes,
            "success_rate": self.success_rate,
        }


@dataclass
class RoutingTable:
    """Per-task-dimension model routing table with epsilon-greedy exploration.

    Attributes:
        models: the set of candidate models.
        epsilon: exploration probability (0..1).
        rng: random source (injectable for deterministic tests).
    """

    models: List[str]
    epsilon: float = DEFAULT_EPSILON
    rng: random.Random = field(default_factory=random.Random)
    _records: Dict[str, ModelPerformance] = field(default_factory=dict)

    def _key(self, model: str, dimension: str) -> str:
        return f"{model}::{dimension}"

    def record_outcome(self, model: str, dimension: str, success: bool) -> None:
        """Record an execution outcome for a model on a task dimension."""
        if model not in self.models:
            self.models.append(model)
        key = self._key(model, dimension)
        rec = self._records.get(key)
        if rec is None:
            rec = ModelPerformance(model=model, dimension=dimension)
            self._records[key] = rec
        rec.record(success)

    def best_model(self, dimension: str) -> Optional[str]:
        """Return the model with the best success rate on a dimension.

        Ties broken by more attempts (more signal), then insertion order.
        Returns None if no model has a record on the dimension.
        """
        candidates = [
            rec
            for key, rec in self._records.items()
            if rec.dimension == dimension and rec.attempts > 0
        ]
        if not candidates:
            return None
        candidates.sort(
            key=lambda r: (r.success_rate or 0.0, r.attempts),
            reverse=True,
        )
        return candidates[0].model

    def select_model(self, task: Dict[str, Any]) -> Optional[str]:
        """Select a model for a task using epsilon-greedy exploration.

        With probability ``epsilon``, pick a random model (exploration).
        Otherwise pick the best historical model for the task's dimension.
        If no model has a record on the dimension yet, fall back to a random
        model (cold start) so exploration happens naturally.

        Returns None only if there are no models at all.
        """
        if not self.models:
            return None
        dimension = classify_task(task)

        # Exploration.
        if self.rng.random() < self.epsilon:
            return self.rng.choice(self.models)

        # Exploitation.
        best = self.best_model(dimension)
        if best is not None:
            return best

        # Cold start: no signal on this dimension yet — explore.
        return self.rng.choice(self.models)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "models": self.models,
            "epsilon": self.epsilon,
            "records": [rec.to_dict() for rec in self._records.values()],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RoutingTable":
        table = cls(models=list(data.get("models", [])), epsilon=data.get("epsilon", DEFAULT_EPSILON))
        for rec in data.get("records", []):
            m = ModelPerformance(
                model=rec["model"],
                dimension=rec["dimension"],
                attempts=rec["attempts"],
                successes=rec["successes"],
            )
            table._records[table._key(m.model, m.dimension)] = m
        return table