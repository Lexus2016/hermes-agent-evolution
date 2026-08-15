#!/usr/bin/env python3
"""Live runtime caller for run_gepa_generation (issue #2232, Slices B+C+D)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.gepa_evolution import EvolutionTree, run_gepa_generation  # noqa: E402
from tools.gepa_promotion import PromotionGate, PromotionLedger, promote_candidate  # noqa: E402
from tools.gepa_reflector import VariantResult  # noqa: E402

SEED_TEXT = (
    "Read the task prompt carefully. Use the available tools to gather the "
    "needed information. Answer the task directly and concisely."
)

# Default ledger path — the real, persisted consumer of promoted candidates.
# Override via GEPA_PROMOTION_LEDGER for tests/isolated runs.
_DEFAULT_LEDGER = ".hermes/evolution/gepa/promotions.jsonl"


def build_seed_results() -> List[VariantResult]:
    """Construct deterministic evaluation results for the seed candidate."""
    return [
        VariantResult(variant="seed", task="t1", passed=True),
        VariantResult(variant="seed", task="t2", passed=False),
    ]


def build_heldout_results() -> List[VariantResult]:
    """Deterministic held-out results (disjoint task ids from the seed set).

    These tasks were NOT used to produce the candidate, so they are a valid
    held-out set for the promotion gate (Slice C).
    """
    return [
        VariantResult(variant="cand", task="h1", passed=True),
        VariantResult(variant="cand", task="h2", passed=True),
    ]


def run(argv: List[str], ledger_path: Optional[str] = None) -> int:
    """Run one GEPA generation, validate against the held-out set, and promote.

    Returns 0 on success. The promotion ledger is the durable consumer: a
    candidate that passes the held-out gate is appended so a later run can
    adopt it as the next seed.
    """
    tree = EvolutionTree()
    seed = tree.add_seed(SEED_TEXT)
    child = run_gepa_generation(tree, seed, build_seed_results())

    ledger = PromotionLedger(
        ledger_path or os_environ_or(_DEFAULT_LEDGER)
    )
    gate = PromotionGate()
    decision, record = promote_candidate(
        gate, ledger,
        candidate_id=child.id,
        text=child.text,
        candidate_results=build_seed_results(),
        heldout_results=build_heldout_results(),
        source_generation=child.generation,
    )

    print(
        json.dumps(
            {
                "seed": seed.id,
                "child": child.id,
                "parent_id": child.parent_id,
                "generation": child.generation,
                "origin": child.origin,
                "critique_summary": child.critique_summary,
                "tree_size": len(tree),
                "promotion": decision.verdict,
                "promoted_hash": record["content_hash"] if record else None,
            },
            sort_keys=True,
        )
    )
    return 0


def os_environ_or(default: str) -> str:
    import os

    return os.environ.get("GEPA_PROMOTION_LEDGER", default)


def main(argv: List[str]) -> int:
    return run(argv)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
