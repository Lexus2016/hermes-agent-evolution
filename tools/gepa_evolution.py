#!/usr/bin/env python3
"""GEPA mutation + tree accumulation (issue #2232, Slice B)."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from tools.gepa_reflector import Critique, VariantResult, reflect


@dataclass
class Candidate:
    """A skill variant in the evolution tree."""

    id: str
    text: str
    parent_id: Optional[str] = None
    generation: int = 0
    origin: str = "seed"
    critique_summary: str = ""
    selected: bool = False
    pruned: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)


class EvolutionTree:
    """Auditable tree of evolved candidates with parent-child links."""

    def __init__(self) -> None:
        self._nodes: Dict[str, Candidate] = {}

    def add(self, c: Candidate) -> Candidate:
        if c.id in self._nodes:
            raise ValueError(f"duplicate candidate id: {c.id}")
        self._nodes[c.id] = c
        return c

    def add_seed(self, text: str) -> Candidate:
        return self.add(Candidate(id=self._new_id(text, "seed"), text=text, generation=0))

    def __len__(self) -> int:
        return len(self._nodes)

    @staticmethod
    def _new_id(text: str, salt: str = "") -> str:
        return hashlib.sha256(f"{salt}:{text}".encode()).hexdigest()[:12]


def _critique_signals(critiques: List[Critique]) -> Tuple[List[str], List[str]]:
    success, failure = [], []
    for c in critiques:
        for s in c.signals:
            tag = s.lower()
            if "fail" in tag:
                failure.append(tag)
            elif "success" in tag or "pass" in tag:
                success.append(tag)
    return success, failure


MutatorCallback = Callable[[str, List[Critique]], str]


def _deterministic_mutate(text: str, critiques: List[Critique]) -> str:
    succ, fail = _critique_signals(critiques)
    parts = []
    if succ:
        parts.append(f"# Reinforced: {', '.join(sorted(set(succ)))}")
    if fail:
        parts.append(f"# Corrected: {', '.join(sorted(set(fail)))}")
    if not parts:
        parts.append("# No critique signals — minor refinement")
    stamp = "\n".join(parts)
    return text if text.rstrip().endswith(stamp.rstrip()) else f"{text.rstrip()}\n\n{stamp}"


def mutate(
    text: str, critiques: List[Critique], *, llm: Optional[MutatorCallback] = None
) -> str:
    if llm is not None:
        try:
            return llm(text, critiques)
        except Exception:  # pragma: no cover
            pass
    return _deterministic_mutate(text, critiques)


def run_gepa_generation(
    tree: EvolutionTree,
    parent: Candidate,
    results: List[VariantResult],
    *,
    mutate_llm: Optional[MutatorCallback] = None,
    reflect_llm: Optional[Callable] = None,
) -> Candidate:
    critiques = reflect(results, llm=reflect_llm)
    new_text = mutate(parent.text, critiques, llm=mutate_llm)
    succ, fail = _critique_signals(critiques)
    summary = "; ".join(
        f"{k}:{','.join(sorted(set(v)))}" for k, v in (("reinforce", succ), ("correct", fail)) if v
    ) or "no-signals"
    return tree.add(
        Candidate(
            id=tree._new_id(new_text, f"mut-{parent.id}"),
            text=new_text,
            parent_id=parent.id,
            generation=parent.generation + 1,
            origin="mutate",
            critique_summary=summary,
            metadata={
                "success_signals": sorted(set(succ)),
                "failure_signals": sorted(set(fail)),
            },
        )
    )
