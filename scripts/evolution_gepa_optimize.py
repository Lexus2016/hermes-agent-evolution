#!/usr/bin/env python3
"""Live runtime caller for GEPA generation + held-out validation (#2232, Slice B+C)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.gepa_evolution import EvolutionTree, run_gepa_generation  # noqa: E402
from tools.gepa_reflector import VariantResult  # noqa: E402
from tools.gepa_validator import promote_if_valid, validate_held_out  # noqa: E402

SEED_TEXT = (
    "Read the task prompt carefully. Use the available tools to gather the "
    "needed information. Answer the task directly and concisely."
)


def build_seed_results() -> List[VariantResult]:
    """Deterministic *training* evaluation results for the seed candidate."""
    return [
        VariantResult(variant="seed", task="train-1", passed=True),
        VariantResult(variant="seed", task="train-2", passed=False),
    ]


def build_held_out_results(variant: str = "child") -> List[VariantResult]:
    """Deterministic *held-out* evaluation results (Slice C)."""
    return [
        VariantResult(variant=variant, task="holdout-1", passed=True),
        VariantResult(variant=variant, task="holdout-2", passed=True),
        VariantResult(variant=variant, task="holdout-3", passed=False),
    ]


def run(argv: List[str]) -> int:
    """Run one GEPA generation + held-out validation and print a JSON summary."""
    tree = EvolutionTree()
    seed = tree.add_seed(SEED_TEXT)
    train = build_seed_results()
    child = run_gepa_generation(tree, seed, train)

    held_out = build_held_out_results(variant=child.id)
    result = validate_held_out(child, train, held_out, threshold=0.6)
    promoted = promote_if_valid(child, result)

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
                "held_out_validation": {
                    "passed": result.passed,
                    "pass_rate": round(result.pass_rate, 4),
                    "threshold": result.threshold,
                    "n_held_out": result.n_held_out,
                    "n_passed": result.n_passed,
                },
                "promoted": promoted,
            },
            sort_keys=True,
        )
    )
    return 0


def main(argv: List[str]) -> int:
    return run(argv)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
