"""Tests for gepa_evolution (issue #2232, Slice B)."""

import pytest

from tools.gepa_evolution import (
    Candidate,
    EvolutionTree,
    crossover,
    mutate,
    run_gepa_generation,
)
from tools.gepa_reflector import Critique, VariantResult, reflect


# ── Helpers ──────────────────────────────────────────────────────────────


def _crits(variant: str = "v1", passed: bool = False, n: int = 2):
    return reflect([
        VariantResult(variant=variant, task=f"t{i}", passed=passed) for i in range(n)
    ])


def _mixed_crits():
    return reflect([
        VariantResult(variant="v1", task="t1", passed=True),
        VariantResult(variant="v1", task="t2", passed=False),
    ])


# ── mutate ───────────────────────────────────────────────────────────────


def test_mutate_produces_different_text():
    base = "# Skill\nDo the thing."
    result = mutate(base, _mixed_crits())
    assert result != base
    assert "Skill" in result  # original content preserved


def test_mutate_guided_by_critiques_not_random():
    """Same critiques → same output (deterministic, not random)."""
    crits = _mixed_crits()
    a = mutate("base text", crits)
    b = mutate("base text", crits)
    assert a == b


def test_mutate_different_critiques_different_output():
    """Different critiques must produce different mutations."""
    success_only = reflect([VariantResult(variant="v", task="t", passed=True)])
    failure_only = reflect([VariantResult(variant="v", task="t", passed=False)])
    a = mutate("base text", success_only)
    b = mutate("base text", failure_only)
    assert a != b


def test_mutate_success_critique_adds_reinforce_note():
    crits = reflect([VariantResult(variant="v", task="t", passed=True)])
    result = mutate("base", crits)
    assert "Reinforced" in result


def test_mutate_failure_critique_adds_correct_note():
    crits = reflect([VariantResult(variant="v", task="t", passed=False)])
    result = mutate("base", crits)
    assert "Corrected" in result


def test_mutate_llm_callback_used():
    def fake_llm(text, crits):
        return text + "\n# LLM-driven change"

    result = mutate("base", _mixed_crits(), llm=fake_llm)
    assert "LLM-driven change" in result


def test_mutate_llm_failure_falls_back():
    def broken_llm(text, crits):
        raise RuntimeError("LLM down")

    result = mutate("base", _mixed_crits(), llm=broken_llm)
    assert result != "base"  # deterministic fallback applied


# ── crossover ────────────────────────────────────────────────────────────


def test_crossover_combines_parents():
    a = Candidate(
        id="aaa",
        text="# Section A\ncontent A",
        metadata={"failure_signals": ["failure"]},
    )
    b = Candidate(
        id="bbb",
        text="# Section B\ncontent B",
        metadata={"failure_signals": []},
    )
    child_text = crossover(a, b)
    assert "Crossover child" in child_text
    assert "aaa" in child_text and "bbb" in child_text


def test_crossover_fewer_failures_contribute_tail():
    """Parent with fewer failures should contribute the tail."""
    strong = Candidate(
        id="strong",
        text="# H1\nbody1\n\n# H2\nbody2",
        metadata={"failure_signals": []},
    )
    weak = Candidate(
        id="weak",
        text="# H1\nweak1\n\n# H2\nweak2",
        metadata={"failure_signals": ["failure"]},
    )
    child_text = crossover(weak, strong)
    # strong (b) has fewer failures → contributes tail
    assert "body2" in child_text


# ── EvolutionTree ────────────────────────────────────────────────────────


def test_tree_add_seed_and_lookup():
    tree = EvolutionTree()
    seed = tree.add_seed("seed text")
    assert seed.origin == "seed"
    assert seed.generation == 0
    assert tree.get(seed.id) is seed
    assert len(tree) == 1


def test_tree_children_and_roots():
    tree = EvolutionTree()
    seed = tree.add_seed("seed")
    child = tree.add(
        Candidate(
            id="child1", text="c", parent_id=seed.id, generation=1, origin="mutate"
        )
    )
    assert tree.children(seed.id) == [child]
    assert seed in tree.roots()
    assert child not in tree.roots()


def test_tree_lineage_traces_to_root():
    tree = EvolutionTree()
    seed = tree.add_seed("seed")
    c1 = tree.add(
        Candidate(id="c1", text="c1", parent_id=seed.id, generation=1, origin="mutate")
    )
    c2 = tree.add(
        Candidate(id="c2", text="c2", parent_id=c1.id, generation=2, origin="mutate")
    )
    chain = tree.lineage(c2.id)
    assert [n.id for n in chain] == [seed.id, c1.id, c2.id]


def test_tree_duplicate_id_rejected():
    tree = EvolutionTree()
    tree.add(Candidate(id="x", text="x"))
    with pytest.raises(ValueError, match="duplicate"):
        tree.add(Candidate(id="x", text="y"))


def test_tree_selected_filter():
    tree = EvolutionTree()
    a = tree.add(Candidate(id="a", text="a", selected=True))
    b = tree.add(Candidate(id="b", text="b", selected=False))
    assert tree.selected() == [a]


def test_tree_to_dict_serializes():
    tree = EvolutionTree()
    tree.add_seed("hello")
    d = tree.to_dict()
    assert "nodes" in d
    assert len(d["nodes"]) == 1
    assert d["nodes"][0]["origin"] == "seed"


# ── run_gepa_generation (the live optimization loop) ────────────────────


def test_run_gepa_generation_wires_reflect_mutate_accumulate():
    """The loop must call reflect (Slice A) → mutate → add to tree."""
    tree = EvolutionTree()
    seed = tree.add_seed("# Skill\nDo the thing.")

    results = [
        VariantResult(variant="seed", task="t1", passed=True),
        VariantResult(variant="seed", task="t2", passed=False),
    ]

    child = run_gepa_generation(tree, seed, results)

    assert child.parent_id == seed.id
    assert child.generation == 1
    assert child.origin == "mutate"
    assert child.id != seed.id
    assert child in tree.children(seed.id)
    assert len(tree) == 2  # seed + child


def test_run_gepa_generation_metadata_records_signals():
    tree = EvolutionTree()
    seed = tree.add_seed("base")
    results = [
        VariantResult(variant="seed", task="t1", passed=True),
        VariantResult(variant="seed", task="t2", passed=False),
    ]
    child = run_gepa_generation(tree, seed, results)
    assert "success" in child.metadata["success_signals"]
    assert "failure" in child.metadata["failure_signals"]
    assert child.critique_summary  # non-empty


def test_run_gepa_generation_multiple_generations():
    """Running the loop twice produces a 3-deep lineage (seed → c1 → c2)."""
    tree = EvolutionTree()
    seed = tree.add_seed("base")
    results = [VariantResult(variant="seed", task="t", passed=False)]

    c1 = run_gepa_generation(tree, seed, results)
    c2 = run_gepa_generation(tree, c1, results)

    assert tree.depth() == 2
    assert tree.lineage(c2.id)[0].id == seed.id
