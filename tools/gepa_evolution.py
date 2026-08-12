#!/usr/bin/env python3
"""GEPA mutation/crossover driven by textual critiques + tree accumulation (issue #2232, Slice B).

Implements the **mutate** and **crossover** steps of GEPA (Generative
Evolutionary Program Assembly), building on the **reflect** step already
shipped in ``tools/gepa_reflector.py`` (issue #2231, Slice A).

The reflect step produces natural-language critiques ("why") for each
skill-variant evaluation result. This module consumes those critiques and:

  - **mutate**: produces a new variant by applying a critique-driven edit to a
    parent variant's procedure (a list of steps). The edit is guided by the
    critique's extracted signals (``success``/``failure``) so the mutation
    reinforces what worked and corrects what failed.
  - **crossover**: combines two parent variants by splicing their procedures
    at a compatible point, producing an offspring that inherits steps from
    both parents.
  - **tree accumulation**: maintains a generation tree (parent → offspring
    lineage) so the evolutionary history is preserved and the best-performing
    lineage can be traced back to its root.

This is a **standalone module** — no changes to the existing evaluation path.
It is deterministic (rule-based) by default so it is unit-testable and safe to
run without an LLM. An optional LLM callback can be supplied to produce richer
critique-driven edits.

Input contract (critiques):
    A list of Critique objects from ``gepa_reflector.reflect`` (or plain
    dicts with the same keys: ``variant``, ``task``, ``passed``, ``critique``,
    ``signals``).

Output contract (offspring):
    A new variant whose procedure is a list of steps (strings). The offspring
    is tagged with its parent lineage so the tree can be accumulated.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# ── Data model ────────────────────────────────────────────────────────────

@dataclass
class Variant:
    """A skill variant: a named procedure (list of steps)."""

    name: str
    steps: List[str] = field(default_factory=list)
    parent: Optional[str] = None  # parent variant name (lineage)
    generation: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "steps": self.steps,
            "parent": self.parent,
            "generation": self.generation,
        }


@dataclass
class Offspring:
    """A newly produced variant plus its lineage metadata."""

    variant: Variant
    operation: str  # "mutate" | "crossover"
    parents: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "variant": self.variant.to_dict(),
            "operation": self.operation,
            "parents": self.parents,
        }


# ── Critique-driven mutation ─────────────────────────────────────────────

def _critique_signals(critique: Any) -> List[str]:
    """Extract structured signals from a critique (dict or Critique object)."""
    if isinstance(critique, dict):
        return list(critique.get("signals") or [])
    return list(getattr(critique, "signals", None) or [])


def _critique_text(critique: Any) -> str:
    """Extract the natural-language critique text (dict or Critique object)."""
    if isinstance(critique, dict):
        return str(critique.get("critique") or "")
    return str(getattr(critique, "critique", "") or "")


def _default_mutate(parent: Variant, critique: Any) -> Variant:
    """Deterministic, rule-based mutation guided by a critique's signals.

    - If the critique signals ``success``, the mutation *reinforces* the
      parent by appending a "reinforce" step (the procedure worked; keep it).
    - If the critique signals ``failure``, the mutation *corrects* the parent
      by appending a "correct" step that addresses the failure.
    - Otherwise, a neutral "refine" step is appended.

    This is the no-LLM fallback — deterministic and unit-testable.
    """
    signals = _critique_signals(critique)
    text = _critique_text(critique)
    steps = list(parent.steps)

    if "failure" in signals:
        steps.append(f"correct: {text or 'address the observed failure'}")
    elif "success" in signals:
        steps.append(f"reinforce: {text or 'keep the working procedure'}")
    else:
        steps.append("refine: adjust the procedure based on critique")

    return Variant(
        name=f"{parent.name}-m{parent.generation + 1}",
        steps=steps,
        parent=parent.name,
        generation=parent.generation + 1,
    )


# ── Crossover ─────────────────────────────────────────────────────────────

def _default_crossover(parent_a: Variant, parent_b: Variant) -> Variant:
    """Deterministic single-point crossover of two parent procedures.

    Splices the first half of ``parent_a`` with the second half of
    ``parent_b`` (and vice-versa is not needed for a single offspring). The
    offspring inherits lineage from both parents.
    """
    mid_a = max(1, len(parent_a.steps) // 2)
    mid_b = max(1, len(parent_b.steps) // 2)
    steps = parent_a.steps[:mid_a] + parent_b.steps[mid_b:]
    if not steps:
        steps = list(parent_a.steps) or list(parent_b.steps)
    return Variant(
        name=f"{parent_a.name}x{parent_b.name}-g{max(parent_a.generation, parent_b.generation) + 1}",
        steps=steps,
        parent=parent_a.name,
        generation=max(parent_a.generation, parent_b.generation) + 1,
    )


# ── LLM-backed mutation (optional) ───────────────────────────────────────

MutateCallback = Callable[[Variant, Any], Variant]


def _llm_mutate(
    parent: Variant,
    critique: Any,
    llm: MutateCallback,
) -> Variant:
    """Produce a mutation via an LLM callback, falling back to deterministic."""
    try:
        offspring = llm(parent, critique)
        if isinstance(offspring, Variant):
            return offspring
    except Exception as exc:
        logger.warning("gepa: LLM mutation failed (%s) — falling back", exc)
    return _default_mutate(parent, critique)


# ── Public API ────────────────────────────────────────────────────────────

def mutate(
    parent: Variant,
    critique: Any,
    llm: Optional[MutateCallback] = None,
) -> Offspring:
    """Produce a new variant by mutating *parent* guided by *critique*.

    Args:
        parent: the parent variant to mutate.
        critique: a Critique (or dict) from ``gepa_reflector.reflect``.
        llm: optional LLM callback for richer critique-driven edits.

    Returns:
        An Offspring tagged with the ``mutate`` operation and parent lineage.
    """
    if llm is not None:
        variant = _llm_mutate(parent, critique, llm)
    else:
        variant = _default_mutate(parent, critique)
    return Offspring(variant=variant, operation="mutate", parents=[parent.name])


def crossover(
    parent_a: Variant,
    parent_b: Variant,
    llm: Optional[MutateCallback] = None,
) -> Offspring:
    """Produce a new variant by crossing two parents.

    Args:
        parent_a, parent_b: the two parent variants to combine.
        llm: optional LLM callback (used for mutation-style refinement of the
            spliced offspring; if omitted, deterministic crossover is used).

    Returns:
        An Offspring tagged with the ``crossover`` operation and both parents.
    """
    if llm is not None:
        variant = _llm_mutate(parent_a, _critique_from_crossover(parent_a, parent_b), llm)
    else:
        variant = _default_crossover(parent_a, parent_b)
    return Offspring(variant=variant, operation="crossover", parents=[parent_a.name, parent_b.name])


def _critique_from_crossover(parent_a: Variant, parent_b: Variant) -> Dict[str, Any]:
    """Build a synthetic critique describing a crossover for LLM refinement."""
    return {
        "variant": parent_a.name,
        "task": "crossover",
        "passed": True,
        "critique": f"Combine the strengths of '{parent_a.name}' and '{parent_b.name}'.",
        "signals": ["success", "crossover"],
    }


# ── Tree accumulation ────────────────────────────────────────────────────

class EvolutionTree:
    """Accumulates the generation tree (parent → offspring lineage).

    Tracks every produced variant and its parent(s) so the evolutionary
    history can be traced and the best-performing lineage identified.
    """

    def __init__(self) -> None:
        self._variants: Dict[str, Variant] = {}

    def add(self, offspring: Offspring) -> None:
        """Register an offspring (and its parents) in the tree."""
        for p in offspring.parents:
            if p not in self._variants:
                # Parent not yet registered — create a stub so lineage holds.
                self._variants[p] = Variant(name=p, generation=0)
        self._variants[offspring.variant.name] = offspring.variant

    def get(self, name: str) -> Optional[Variant]:
        return self._variants.get(name)

    def lineage(self, name: str) -> List[str]:
        """Return the ancestor chain from *name* back to its root (inclusive)."""
        chain: List[str] = []
        cur = name
        seen: set = set()
        while cur and cur not in seen:
            seen.add(cur)
            chain.append(cur)
            variant = self._variants.get(cur)
            cur = variant.parent if variant else None
        return chain

    def all(self) -> List[Variant]:
        return list(self._variants.values())

    def to_dict(self) -> Dict[str, Any]:
        return {name: v.to_dict() for name, v in self._variants.items()}


def accumulate(
    tree: EvolutionTree,
    offspring: Offspring,
) -> EvolutionTree:
    """Add an offspring to the tree and return it (convenience wrapper)."""
    tree.add(offspring)
    return tree


def store_offspring(
    offspring: Offspring,
    path: str,
) -> None:
    """Store an offspring as one JSON line (JSONL append) for later replay."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(offspring.to_dict()) + "\n")


def load_offspring(path: str) -> List[Offspring]:
    """Load offspring records from a JSONL file produced by ``store_offspring``."""
    result: List[Offspring] = []
    if not os.path.exists(path):
        return result
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                v = data["variant"]
                result.append(
                    Offspring(
                        variant=Variant(
                            name=v["name"],
                            steps=v.get("steps", []),
                            parent=v.get("parent"),
                            generation=v.get("generation", 0),
                        ),
                        operation=data.get("operation", "mutate"),
                        parents=data.get("parents", []),
                    )
                )
            except (KeyError, ValueError, TypeError) as exc:
                logger.warning("gepa: skipping malformed offspring line: %s", exc)
    return result
