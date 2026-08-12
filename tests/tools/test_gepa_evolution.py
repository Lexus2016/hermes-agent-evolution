"""Tests for tools/gepa_evolution.py — critique-driven mutation/crossover + tree (issue #2232, Slice B)."""

import json

import pytest

from tools.gepa_evolution import (
    EvolutionTree,
    Offspring,
    Variant,
    accumulate,
    crossover,
    load_offspring,
    mutate,
    store_offspring,
)


def _variant(name="v1", steps=None, generation=0):
    return Variant(name=name, steps=steps or ["step1", "step2"], generation=generation)


def _critique(signals, text="critique text"):
    return {"variant": "v1", "task": "t1", "passed": True, "critique": text, "signals": signals}


def test_mutate_success_reinforces():
    off = mutate(_variant(), _critique(["success"]))
    assert isinstance(off, Offspring)
    assert off.operation == "mutate"
    assert off.parents == ["v1"]
    assert off.variant.steps[-1].startswith("reinforce:")


def test_mutate_failure_corrects():
    off = mutate(_variant(), _critique(["failure"]))
    assert off.variant.steps[-1].startswith("correct:")


def test_mutate_neutral_refines():
    off = mutate(_variant(), _critique([]))
    assert off.variant.steps[-1].startswith("refine:")


def test_mutate_increments_generation_and_lineage():
    parent = _variant(generation=2)
    off = mutate(parent, _critique(["success"]))
    assert off.variant.generation == 3
    assert off.variant.parent == "v1"


def test_crossover_splices_both_parents():
    a = _variant("a", ["a1", "a2", "a3", "a4"])
    b = _variant("b", ["b1", "b2", "b3", "b4"])
    off = crossover(a, b)
    assert off.operation == "crossover"
    assert off.parents == ["a", "b"]
    # Offspring inherits first half of a and second half of b.
    assert off.variant.steps[0] == "a1"
    assert off.variant.steps[-1] == "b4"


def test_evolution_tree_accumulates_lineage():
    tree = EvolutionTree()
    root = _variant("root", ["r1"])
    child = mutate(root, _critique(["success"]))
    accumulate(tree, child)
    grandchild = mutate(child.variant, _critique(["failure"]))
    accumulate(tree, grandchild)

    assert tree.get("root") is not None
    assert tree.get(child.variant.name) is not None
    lineage = tree.lineage(grandchild.variant.name)
    assert lineage[0] == grandchild.variant.name
    assert lineage[-1] == "root"


def test_store_and_load_offspring_roundtrip(tmp_path):
    path = str(tmp_path / "offspring.jsonl")
    off = mutate(_variant(), _critique(["success"]))
    store_offspring(off, path)
    loaded = load_offspring(path)
    assert len(loaded) == 1
    assert loaded[0].variant.name == off.variant.name
    assert loaded[0].operation == "mutate"


def test_load_offspring_missing_file_returns_empty(tmp_path):
    assert load_offspring(str(tmp_path / "nope.jsonl")) == []
