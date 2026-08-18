# -*- coding: utf-8 -*-
"""RubricForge Slice 3 — held-out false-pass gate tests (#2782)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from evolution.lib.rubric_heldout_gate import (  # noqa: E402
    held_out_false_pass_gate,
    split_labeled_set,
)


def _kw_judge(rubric: str, example) -> bool:
    """Keyword-requirement judge mirroring the S1 wiring."""
    ex = example if isinstance(example, dict) else {}
    requires = [k for k in (ex.get("requires") or []) if isinstance(k, str)]
    forbids = [k for k in (ex.get("forbids") or []) if isinstance(k, str)]
    verdict = all(k in rubric for k in requires)
    if forbids:
        verdict = verdict and any(k in rubric for k in forbids)
    return bool(verdict)


def test_split_is_deterministic_tail_and_parallel():
    train_x, train_y, held_x, held_y = split_labeled_set(
        list(range(10)), [i % 2 == 0 for i in range(10)], holdout=3
    )
    assert train_x == list(range(7)) and held_x == [7, 8, 9]
    assert held_y == [False, True, False]


def test_split_too_small_degrades_to_empty_holdout():
    _, _, held_x, held_y = split_labeled_set([1], [True], holdout=3)
    assert held_x == [] and held_y == []


def test_mismatched_inputs_raise():
    try:
        split_labeled_set([1, 2], [True])
        raise SystemExit("expected ValueError")
    except ValueError:
        pass


def test_gate_blocks_overfit_evolved_rubric():
    # Generic rubric covers the good examples; the evolved one ALSO waves
    # through a held-out labeled-bad example → higher held-out false-pass.
    labeled = [
        {"requires": ["sources"], "label": True},
        {"requires": ["urls"], "label": True},
        {"requires": ["sources"], "label": True},
        {"requires": ["depth"], "label": True},
        {"requires": ["vibes"], "label": False},  # held-out bad example
        {"requires": ["guesses"], "label": False},
    ]
    labels = [e["label"] for e in labeled]
    generic = "sources urls depth"
    overfit = "sources urls depth vibes guesses"  # accepts the bad ones
    verdict = held_out_false_pass_gate(overfit, generic, labeled, labels, _kw_judge)
    assert verdict.adopt is False
    assert verdict.evolved_false_pass_rate == 1.0
    assert verdict.baseline_false_pass_rate == 0.0
    assert "overfit" in verdict.reason


def test_gate_admits_parity_or_better():
    labeled = [
        {"requires": ["a"]},
        {"requires": ["b"]},
        {"requires": ["c"]},
        {"requires": ["d"]},
    ]
    labels = [True, True, False, False]
    generic = evolved = "a b c d"  # identical → parity
    verdict = held_out_false_pass_gate(evolved, generic, labeled, labels, _kw_judge)
    assert verdict.adopt is True
    assert verdict.evolved_false_pass_rate == verdict.baseline_false_pass_rate


def test_empty_holdout_fails_closed():
    verdict = held_out_false_pass_gate("x", "y", [1], [True], _kw_judge)
    assert verdict.adopt is False
    assert "refusing" in verdict.reason


def test_judge_throw_counts_as_no_false_pass():
    def boom(rubric, example):
        raise RuntimeError("gate crash")

    labeled = [{"requires": ["a"]}, {"requires": ["b"]}]
    labels = [True, False]
    verdict = held_out_false_pass_gate("a b", "", labeled, labels, boom)
    # A throwing judge accepts nothing → 0 false passes on both sides → parity.
    assert verdict.adopt is True


def test_judge_consumer_blocks_overfit_adoption(tmp_path):
    """End-to-end through resolve_active_rubric (#2782 wiring): an evolved
    rubric that wins the labeled set but false-passes on the held-out tail
    is NOT adopted — the judge keeps the shipped rubric (None)."""
    import json

    from evolution_rubric_judge import resolve_active_rubric

    rf = tmp_path / "rubric-forge"
    rf.mkdir(parents=True)
    # 4 train examples the evolved rubric fits perfectly...
    (rf / "candidates.json").write_text(
        json.dumps(["sources urls depth vibes guesses"]), encoding="utf-8"
    )
    labeled = (
        [{"requires": [w], "label": True} for w in ("sources", "urls", "depth")]
        + [{"requires": ["vibes"], "label": False}]      # train bad
        + [{"requires": ["guesses"], "label": False}]    # held-out bad
    )
    (rf / "labeled.json").write_text(json.dumps(labeled), encoding="utf-8")

    active = resolve_active_rubric(tmp_path)
    # The winner accepts the held-out bad example → gate blocks adoption.
    assert active is None
    audit = json.loads((rf / "selected.json").read_text())
    assert audit["held_out_gate"]["adopt"] is False
    assert audit["held_out_gate"]["evolved_false_pass_rate"] == 1.0
    assert audit["max_overrides"] == {}
