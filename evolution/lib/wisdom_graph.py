# -*- coding: utf-8 -*-
"""Wisdom Graph & PCR Triplet Representation for evolution pipeline (issue #2385).

Adopts the MEGA framework (arXiv:2608.10504, 'MEGA: Wisdom Graph as a Unified
Self-Evolving Agent Infrastructure'):
1. Decomposes distilled insights into atomic PCR (Primary-Context-Resultant) triplets.
2. Maintains a typed Wisdom Graph with Sufficiency and Necessity relational scores.
3. Supports deductive, inductive, and abductive inference over evolution findings.
4. Provides Seed-Epoch attribution to isolate strategy gains from random seed variance.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

__all__ = [
    "PCRTriplet",
    "WisdomGraph",
]


def _canonical_id(primary: str, context: str, resultant: str) -> str:
    """Generate a deterministic 12-char hex ID for a PCR triplet."""
    payload = f"{primary.strip().lower()}::{context.strip().lower()}::{resultant.strip().lower()}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


@dataclass
class PCRTriplet:
    """Atomic Primary-Context-Resultant wisdom triplet.

    Attributes:
        primary_insight: Core finding, mechanism, or principle.
        context_condition: Context, preconditions, or domain where it holds.
        resultant_action: Concrete actionable directive or outcome.
        sufficiency_score: 0.0-1.0 degree to which Context is sufficient for Resultant.
        necessity_score: 0.0-1.0 degree to which Primary insight is necessary for Resultant.
        provenance_sources: List of source URLs, papers, or file paths.
        triplet_id: Deterministic identifier.
    """

    primary_insight: str
    context_condition: str
    resultant_action: str
    sufficiency_score: float = 1.0
    necessity_score: float = 1.0
    provenance_sources: List[str] = field(default_factory=list)
    triplet_id: str = ""

    def __post_init__(self) -> None:
        if not self.triplet_id:
            self.triplet_id = _canonical_id(
                self.primary_insight, self.context_condition, self.resultant_action
            )
        self.sufficiency_score = max(0.0, min(1.0, float(self.sufficiency_score)))
        self.necessity_score = max(0.0, min(1.0, float(self.necessity_score)))

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> PCRTriplet:
        return cls(
            primary_insight=str(d.get("primary_insight", "")),
            context_condition=str(d.get("context_condition", "")),
            resultant_action=str(d.get("resultant_action", "")),
            sufficiency_score=float(d.get("sufficiency_score", 1.0)),
            necessity_score=float(d.get("necessity_score", 1.0)),
            provenance_sources=list(d.get("provenance_sources", []) or []),
            triplet_id=str(d.get("triplet_id", "")),
        )


class WisdomGraph:
    """Relational Wisdom Graph storing PCR triplets with multi-directional inference."""

    def __init__(self) -> None:
        self.triplets: Dict[str, PCRTriplet] = {}

    def add_triplet(self, triplet: PCRTriplet) -> str:
        """Add or update a PCR triplet."""
        self.triplets[triplet.triplet_id] = triplet
        return triplet.triplet_id

    def get_triplet(self, triplet_id: str) -> Optional[PCRTriplet]:
        return self.triplets.get(triplet_id)

    def extract_pcr_from_text(
        self,
        text: str,
        provenance: Optional[str] = None,
    ) -> PCRTriplet:
        """Heuristic rule-based extraction of PCR components from structured text."""
        cleaned = text.strip()
        lines = [line.strip() for line in cleaned.splitlines() if line.strip()]

        primary = ""
        context = ""
        resultant = ""

        for line in lines:
            lower = line.lower()
            if lower.startswith(("primary:", "insight:", "principle:", "finding:")):
                primary = line.split(":", 1)[1].strip()
            elif lower.startswith(("context:", "condition:", "when:", "if:")):
                context = line.split(":", 1)[1].strip()
            elif lower.startswith(("resultant:", "action:", "then:", "directive:")):
                resultant = line.split(":", 1)[1].strip()

        if not primary:
            primary = lines[0] if lines else "General insight"
        if not context:
            context = "General execution"
        if not resultant:
            resultant = lines[-1] if len(lines) > 1 else primary

        sources = [provenance] if provenance else []
        triplet = PCRTriplet(
            primary_insight=primary,
            context_condition=context,
            resultant_action=resultant,
            provenance_sources=sources,
        )
        self.add_triplet(triplet)
        return triplet

    def query(
        self,
        query_text: str,
        top_k: int = 5,
        min_sufficiency: float = 0.0,
    ) -> List[PCRTriplet]:
        """Compositional retrieval matching query terms against Primary, Context, and Resultant."""
        tokens = set(re.findall(r"\w+", query_text.lower()))
        if not tokens:
            return list(self.triplets.values())[:top_k]

        scored: List[Tuple[float, PCRTriplet]] = []
        for triplet in self.triplets.values():
            if triplet.sufficiency_score < min_sufficiency:
                continue

            content = f"{triplet.primary_insight} {triplet.context_condition} {triplet.resultant_action}".lower()
            content_tokens = set(re.findall(r"\w+", content))
            overlap = len(tokens & content_tokens)

            # Combined score: term overlap weighted by sufficiency * necessity
            rel_score = (triplet.sufficiency_score + triplet.necessity_score) / 2.0
            total_score = overlap * (1.0 + rel_score)
            if overlap > 0:
                scored.append((total_score, triplet))

        scored.sort(key=lambda item: item[0], reverse=True)
        return [t for _, t in scored[:top_k]]

    def deductive_reasoning(self, context: str) -> List[str]:
        """Deduce recommended resultant actions given an active context."""
        matched_triplets = self.query(context, top_k=10)
        actions: List[str] = []
        seen: Set[str] = set()
        for t in matched_triplets:
            if t.resultant_action and t.resultant_action not in seen:
                seen.add(t.resultant_action)
                actions.append(t.resultant_action)
        return actions

    def abductive_reasoning(self, observed_result: str) -> List[PCRTriplet]:
        """Abduce potential primary causes/contexts from an observed outcome or error."""
        tokens = set(re.findall(r"\w+", observed_result.lower()))
        hypotheses: List[PCRTriplet] = []
        for t in self.triplets.values():
            r_tokens = set(re.findall(r"\w+", t.resultant_action.lower()))
            if tokens & r_tokens:
                hypotheses.append(t)
        return hypotheses

    @staticmethod
    def seed_epoch_attribution(
        eval_runs: Sequence[Dict[str, Any]],
    ) -> Dict[str, float]:
        """Fixed-seed attribution: isolate strategy-driven performance gains from seed variance.

        Each eval_run should have:
            {"strategy": str, "seed": int, "score": float}
        Returns:
            {strategy_name: mean_isolated_gain}
        """
        # Group scores by seed
        seed_baseline: Dict[int, List[float]] = {}
        strategy_seed_scores: Dict[str, Dict[int, List[float]]] = {}

        for run in eval_runs:
            strat = str(run.get("strategy", "default"))
            seed = int(run.get("seed", 0))
            score = float(run.get("score", 0.0))

            if seed not in seed_baseline:
                seed_baseline[seed] = []
            seed_baseline[seed].append(score)

            if strat not in strategy_seed_scores:
                strategy_seed_scores[strat] = {}
            if seed not in strategy_seed_scores[strat]:
                strategy_seed_scores[strat][seed] = []
            strategy_seed_scores[strat][seed].append(score)

        # Compute average score per seed across all strategies
        seed_means = {
            seed: sum(scores) / len(scores)
            for seed, scores in seed_baseline.items()
            if scores
        }

        # Calculate seed-isolated attribution for each strategy
        attribution: Dict[str, float] = {}
        for strat, seed_dict in strategy_seed_scores.items():
            diffs: List[float] = []
            for seed, scores in seed_dict.items():
                strat_mean = sum(scores) / len(scores)
                base_mean = seed_means.get(seed, 0.0)
                diffs.append(strat_mean - base_mean)
            attribution[strat] = round(sum(diffs) / len(diffs), 4) if diffs else 0.0

        return attribution

    def to_dict(self) -> Dict[str, Any]:
        return {"triplets": [t.to_dict() for t in self.triplets.values()]}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> WisdomGraph:
        wg = cls()
        for t_data in d.get("triplets", []) or []:
            wg.add_triplet(PCRTriplet.from_dict(t_data))
        return wg

    def save_json(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2, ensure_ascii=False)

    @classmethod
    def load_json(cls, path: str | Path) -> WisdomGraph:
        p = Path(path)
        if not p.exists():
            return cls()
        with open(p, "r", encoding="utf-8") as f:
            return cls.from_dict(json.load(f))
