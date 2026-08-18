# -*- coding: utf-8 -*-
"""RubricForge Slice 2 — labeled-set collection tests (#2781)."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from evolution.lib.rubric_labeled_set import (  # noqa: E402
    append_labeled_examples,
    default_store_path,
    load_labeled_set,
)
from evolution_rubric_judge import resolve_active_rubric  # noqa: E402


def test_append_validates_and_persists(tmp_path):
    store = tmp_path / "labeled.json"
    added = append_labeled_examples(
        [
            {"requires": ["provenance"], "label": True},
            {"forbids": ["vibes"], "label": False},
            "not a dict",          # dropped: wrong type
            {"label": True},       # dropped: no requires/forbids signal
        ],
        store_path=store,
    )
    assert added == 2
    persisted = load_labeled_set(store)
    assert persisted == [
        {"requires": ["provenance"], "forbids": [], "label": True},
        {"requires": [], "forbids": ["vibes"], "label": False},
    ]


def test_duplicates_are_skipped_across_appends(tmp_path):
    store = tmp_path / "labeled.json"
    ex = {"requires": ["urls"], "label": True}
    assert append_labeled_examples([ex], store_path=store) == 1
    assert append_labeled_examples([ex], store_path=store) == 0  # dup
    # Same signal, opposite label = a DIFFERENT example (kept).
    assert append_labeled_examples(
        [{"requires": ["urls"], "label": False}], store_path=store
    ) == 1
    assert len(load_labeled_set(store)) == 2


def test_store_is_bounded_to_newest(tmp_path):
    store = tmp_path / "labeled.json"
    batch = [{"requires": [f"k{i}"], "label": i % 2 == 0} for i in range(10)]
    assert append_labeled_examples(batch, store_path=store, max_examples=4) == 10
    kept = load_labeled_set(store)
    assert len(kept) == 4
    assert kept[0]["requires"] == ["k6"]  # newest 4: k6..k9


def test_default_store_path_is_what_the_judge_reads(tmp_path, monkeypatch):
    monkeypatch.setenv("EVOLUTION_PROFILE_DIR", str(tmp_path))
    assert default_store_path() == tmp_path / "rubric-forge" / "labeled.json"
    # Round-trip with the S1 consumer: collect, then the judge reads it.
    append_labeled_examples([{"requires": ["sources"], "label": True}])
    (tmp_path / "rubric-forge" / "candidates.json").write_text(
        json.dumps(["mentions sources"]), encoding="utf-8"
    )
    active = resolve_active_rubric(tmp_path)
    assert active is not None and active["agreement"] == 1.0
