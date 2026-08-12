"""Tests for scripts/evolution_gepa_optimize.py — live GEPA caller (#2232)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.evolution_gepa_optimize import main  # noqa: E402
from tools.gepa_evolution import EvolutionTree, run_gepa_generation  # noqa: E402
from tools.gepa_reflector import VariantResult  # noqa: E402


def test_run_gepa_generation_adds_child():
    tree = EvolutionTree()
    seed = tree.add_seed("baseline procedure text")
    child = run_gepa_generation(tree, seed, [VariantResult("seed", "t1", True)])
    assert len(tree) == 2
    assert child.parent_id == seed.id
    assert child.origin == "mutate"
    assert child.generation == 1


def test_main_returns_zero():
    assert main(["evolution_gepa_optimize.py"]) == 0
