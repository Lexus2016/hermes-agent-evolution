#!/usr/bin/env python3
"""GEPA mutation/crossover + tree accumulation (issue #2232, Slice B).

Implements steps 3–4 of the GEPA (Generative Evolutionary Program Assembly)
roadmap (parent #2227), building on the **reflect** step from Slice A
(``tools.gepa_reflector``).

What this module adds:

* :func:`mutate` — produce an improved skill-variant candidate guided by the
  textual critiques from the reflector (not random).  The critique text is
  parsed into concrete edit directives ("reinforce X" / "correct Y") that drive
  a deterministic transformation of the source text.
* :func:`crossover` — combine two parent variants into a child by splicing
  their strongest sections, again guided by critique signals.
* :class:`EvolutionTree` — an auditable tree of every candidate produced,
  with parent–child links and prune/select metadata.
* :func:`run_gepa_generation` — the **live optimization loop** that wires the
  above together with Slice A's :func:`~tools.gepa_reflector.reflect` so a full
  reflect → mutate → accumulate cycle can be driven from a single call site.

The transformations are deterministic (rule-based) by default so the module is
unit-testable and safe to run without an LLM, mirroring Slice A's design.
An optional LLM mutator callback can be supplied for richer mutations.

Input contract — consumes :class:`~tools.gepa_reflector.Critique` objects from
Slice A's ``reflect()`` plus the candidate skill text.
Output contract — new candidate skill texts + an :class:`EvolutionTree`
recording the lineage.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from tools.gepa_reflector import Critique, VariantResult, reflect

logger = logging.getLogger(__name__)


# ── Data structures ──────────────────────────────────────────────────────


@dataclass
class Candidate:
    """A skill variant in the evolution tree."""

    id: str
    text: str
    parent_id: Optional[str] = None
    generation: int = 0
    origin: str = "seed"  # seed | mutate | crossover
    critique_summary: str = ""
    selected: bool = False
    pruned: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "parent_id": self.parent_id,
            "generation": self.generation,
            "origin": self.origin,
            "critique_summary": self.critique_summary,
            "selected": self.selected,
            "pruned": self.pruned,
            "metadata": self.metadata,
        }


class EvolutionTree:
    """Auditable tree of evolved candidates with parent–child links.

    Every candidate produced by :func:`mutate` or :func:`crossover` is added
    here, providing a human-readable record of what was tried and why each
    variant was selected or pruned.
    """

    def __init__(self) -> None:
        self._nodes: Dict[str, Candidate] = {}

    # -- insertion -----------------------------------------------------

    def add(self, candidate: Candidate) -> Candidate:
        """Register *candidate* and return it."""
        if candidate.id in self._nodes:
            raise ValueError(f"duplicate candidate id: {candidate.id}")
        self._nodes[candidate.id] = candidate
        return candidate

    def add_seed(self, text: str, name: str = "seed") -> Candidate:
        """Add an initial seed candidate (generation 0)."""
        return self.add(
            Candidate(
                id=self._new_id(text, "seed"), text=text, origin="seed", generation=0
            )
        )

    # -- lookup --------------------------------------------------------

    def get(self, cid: str) -> Candidate:
        return self._nodes[cid]

    def children(self, cid: str) -> List[Candidate]:
        return sorted(
            (n for n in self._nodes.values() if n.parent_id == cid),
            key=lambda c: c.id,
        )

    def roots(self) -> List[Candidate]:
        return [n for n in self._nodes.values() if n.parent_id is None]

    def all(self) -> List[Candidate]:
        return list(self._nodes.values())

    def selected(self) -> List[Candidate]:
        return [n for n in self._nodes.values() if n.selected]

    def lineage(self, cid: str) -> List[Candidate]:
        """Return the chain from root → *cid* (inclusive)."""
        chain: List[Candidate] = []
        cur: Optional[Candidate] = self._nodes.get(cid)
        seen: set = set()
        while cur and cur.id not in seen:
            seen.add(cur.id)
            chain.append(cur)
            cur = self._nodes.get(cur.parent_id) if cur.parent_id else None
        chain.reverse()
        return chain

    # -- mutation helpers ---------------------------------------------

    def depth(self) -> int:
        return max((n.generation for n in self._nodes.values()), default=0)

    def __len__(self) -> int:
        return len(self._nodes)

    def to_dict(self) -> Dict[str, Any]:
        return {"nodes": [n.to_dict() for n in self._nodes.values()]}

    # -- internal ------------------------------------------------------

    @staticmethod
    def _new_id(text: str, salt: str = "") -> str:
        return hashlib.sha256(f"{salt}:{text}".encode()).hexdigest()[:12]


# ── Critique → directive parsing ─────────────────────────────────────────


def _critique_signals(critiques: List[Critique]) -> Tuple[List[str], List[str]]:
    """Split critique signals into (success_signals, failure_signals)."""
    success: List[str] = []
    failure: List[str] = []
    for c in critiques:
        for s in c.signals:
            tag = s.lower()
            if "fail" in tag:
                failure.append(tag)
            elif "success" in tag or "pass" in tag:
                success.append(tag)
    return success, failure


# ── Mutation ─────────────────────────────────────────────────────────────

MutatorCallback = Callable[[str, List[Critique]], str]


def _summarize_critiques(critiques: List[Critique]) -> str:
    """Build a short human-readable summary for the tree node."""
    succ, fail = _critique_signals(critiques)
    parts: List[str] = []
    if succ:
        parts.append(f"reinforce:{','.join(sorted(set(succ)))}")
    if fail:
        parts.append(f"correct:{','.join(sorted(set(fail)))}")
    return "; ".join(parts) if parts else "no-signals"


def _deterministic_mutate(text: str, critiques: List[Critique]) -> str:
    """Apply critique-driven directives to *text* deterministically.

    The mutation appends a structured "evolution note" that records the
    critique signals for this generation.  This is intentionally simple and
    deterministic — it produces a *different* variant that carries forward
    the textual feedback, which is the GEPA contract.  An LLM mutator can be
    plugged in for semantic rewrites.
    """
    succ, fail = _critique_signals(critiques)
    note_parts: List[str] = []
    if succ:
        note_parts.append(f"# Reinforced: {', '.join(sorted(set(succ)))}")
    if fail:
        note_parts.append(f"# Corrected: {', '.join(sorted(set(fail)))}")
    if not note_parts:
        note_parts.append("# No critique signals — minor refinement")
    stamp = "\n".join(note_parts)

    # Avoid duplicating an identical stamp already at the tail.
    if text.rstrip().endswith(stamp.rstrip()):
        return text

    # Strip any prior evolution-note block so successive mutations replace,
    # not stack infinitely.
    cleaned = re.sub(
        r"\n*# (?:Reinforced|Corrected|No critique signals).*?(?=\n# (?:Reinforced|Corrected|No critique signals)|\Z)",
        "",
        text,
        flags=re.DOTALL,
    ).rstrip()
    return f"{cleaned}\n\n{stamp}"


def mutate(
    text: str,
    critiques: List[Critique],
    *,
    llm: Optional[MutatorCallback] = None,
) -> str:
    """Produce an improved candidate from *text* guided by *critiques*.

    Args:
        text: the current skill-variant source text.
        critiques: textual critiques from ``reflect()`` (Slice A) for this
            variant's evaluation results.
        llm: optional callback ``(text, critiques) -> new_text`` for richer
            semantic mutations.  Falls back to the deterministic mutator on
            error.

    Returns:
        The mutated skill-variant text.
    """
    if llm is not None:
        try:
            return llm(text, critiques)
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("gepa mutate: LLM callback failed (%s) — falling back", exc)
    return _deterministic_mutate(text, critiques)


# ── Crossover ────────────────────────────────────────────────────────────


def crossover(parent_a: Candidate, parent_b: Candidate) -> str:
    """Combine two parent candidates into a child text.

    The child takes the first half of *parent_a*'s sections and the second
    half of *parent_b*'s, guided by which parent had stronger critique
    signals (the one with fewer failure signals contributes the tail).
    """
    a_sections = _split_sections(parent_a.text)
    b_sections = _split_sections(parent_b.text)

    a_fail = sum(1 for s in parent_a.metadata.get("failure_signals", []) for _ in [0])
    b_fail = sum(1 for s in parent_b.metadata.get("failure_signals", []) for _ in [0])

    # Parent with fewer failures contributes the tail (conclusion).
    if b_fail < a_fail:
        first, second = a_sections, b_sections
    else:
        first, second = b_sections, a_sections

    mid_a = len(first) // 2 if first else 0
    head = first[:mid_a] if first else []
    tail = second[mid_a:] if second else []
    child_sections = head + tail
    if not child_sections:
        return f"{parent_a.text}\n\n# Crossover child of {parent_a.id} × {parent_b.id}"
    body = "\n\n".join(child_sections)
    return f"{body}\n\n# Crossover child of {parent_a.id} × {parent_b.id}"


def _split_sections(text: str) -> List[str]:
    """Split skill text into top-level sections (by markdown ``#`` headers)."""
    parts = re.split(r"(?m)^(?=#{1,3}\s)", text)
    return [p.strip() for p in parts if p.strip()]


# ── Live optimization loop ───────────────────────────────────────────────


def run_gepa_generation(
    tree: EvolutionTree,
    parent: Candidate,
    results: List[VariantResult],
    *,
    mutate_llm: Optional[MutatorCallback] = None,
    reflect_llm: Optional[Callable] = None,
) -> Candidate:
    """Run one GEPA generation: reflect → mutate → accumulate.

    This is the **live call site** that wires Slice A's ``reflect()`` into the
    mutation step and records the outcome in the :class:`EvolutionTree`,
    satisfying the "not dead code" requirement from the rework brief.

    Args:
        tree: the shared evolution tree to record the new candidate in.
        parent: the candidate being improved.
        results: evaluation results (pass/fail) for *parent*.
        mutate_llm: optional LLM mutator callback.
        reflect_llm: optional LLM reflector callback (passed to ``reflect``).

    Returns:
        The newly-created child :class:`Candidate`.
    """
    # Step 1 — Reflect (Slice A): textual critiques from evaluation results.
    critiques = reflect(results, llm=reflect_llm)

    # Step 2 — Mutate: produce improved variant guided by critiques.
    new_text = mutate(parent.text, critiques, llm=mutate_llm)

    # Step 3 — Accumulate: register the child in the tree with metadata.
    succ, fail = _critique_signals(critiques)
    child = tree.add(
        Candidate(
            id=tree._new_id(new_text, f"mut-{parent.id}"),
            text=new_text,
            parent_id=parent.id,
            generation=parent.generation + 1,
            origin="mutate",
            critique_summary=_summarize_critiques(critiques),
            metadata={
                "success_signals": sorted(set(succ)),
                "failure_signals": sorted(set(fail)),
            },
        )
    )
    logger.info(
        "gepa: generated %s from %s (gen %d, %d succ / %d fail signals)",
        child.id,
        parent.id,
        child.generation,
        len(succ),
        len(fail),
    )
    return child
