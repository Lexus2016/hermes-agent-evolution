#!/usr/bin/env python3
"""Live runtime caller for run_gepa_generation (issue #2232, Slice B)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.gepa_evolution import EvolutionTree, run_gepa_generation  # noqa: E402
from tools.gepa_reflector import VariantResult  # noqa: E402

SEED_TEXT = (
    "Read the task prompt carefully. Use the available tools to gather the "
    "needed information. Answer the task directly and concisely."
)


def build_seed_results() -> List[VariantResult]:
    """Construct deterministic evaluation results for the seed candidate."""
    return [
        VariantResult(variant="seed", task="t1", passed=True),
        VariantResult(variant="seed", task="t2", passed=False),
    ]


def run(argv: List[str]) -> int:
    """Run one GEPA generation and print a JSON summary."""
    tree = EvolutionTree()
    seed = tree.add_seed(SEED_TEXT)
    child = run_gepa_generation(tree, seed, build_seed_results())
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
            },
            sort_keys=True,
        )
    )
    return 0


def main(argv: List[str]) -> int:
    return run(argv)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
