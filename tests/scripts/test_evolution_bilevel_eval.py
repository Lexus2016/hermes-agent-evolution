# -*- coding: utf-8 -*-
"""Tests for scripts/evolution_bilevel_eval.py (#1166)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import evolution_bilevel_eval as bl  # noqa: E402


def _cases():
    return [
        bl.EvalCase("p1", "reliability", bl.Split.public),
        bl.EvalCase("p2", "planning", bl.Split.public),
        bl.EvalCase("v1", "reliability", bl.Split.private),
        bl.EvalCase("v2", "planning", bl.Split.private),
    ]


def test_partition_scores_splits_by_case():
    pub, priv = bl.partition_scores(
        _cases(), {"p1": 1.0, "v1": 0.5, "p2": 0.8, "v2": 0.4}
    )
    assert set(pub) == {"p1", "p2"}
    assert set(priv) == {"v1", "v2"}


def test_go_when_private_beats_incumbent():
    d = bl.bilevel_decision(
        _cases(),
        candidate_scores={"p1": 1.0, "p2": 1.0, "v1": 0.9, "v2": 0.9},
        incumbent_scores={"p1": 0.8, "p2": 0.8, "v1": 0.7, "v2": 0.7},
        budget=bl.CostBudget(max_tokens=1000, spent=500),
    )
    assert d.go is True
    assert d.private_delta > 0


def test_reject_reward_hacking_public_up_private_down():
    d = bl.bilevel_decision(
        _cases(),
        candidate_scores={
            "p1": 1.0,
            "p2": 1.0,
            "v1": 0.3,
            "v2": 0.3,
        },  # public up, private down
        incumbent_scores={"p1": 0.7, "p2": 0.7, "v1": 0.6, "v2": 0.6},
        budget=bl.CostBudget(max_tokens=1000, spent=100),
    )
    assert d.go is False
    assert d.reward_hacking_suspected is True
    assert "reward-hacking" in d.reason


def test_reject_over_budget():
    d = bl.bilevel_decision(
        _cases(),
        candidate_scores={"p1": 1.0, "p2": 1.0, "v1": 0.9, "v2": 0.9},
        incumbent_scores={"p1": 0.5, "p2": 0.5, "v1": 0.5, "v2": 0.5},
        budget=bl.CostBudget(max_tokens=100, spent=500),  # over budget
    )
    assert d.go is False
    assert d.over_budget is True


def test_reject_when_private_not_better():
    d = bl.bilevel_decision(
        _cases(),
        candidate_scores={"p1": 1.0, "p2": 1.0, "v1": 0.5, "v2": 0.5},
        incumbent_scores={"p1": 0.5, "p2": 0.5, "v1": 0.5, "v2": 0.5},  # private tie
        budget=bl.CostBudget(max_tokens=1000, spent=100),
    )
    assert d.go is False


def test_reject_class_regression():
    # private overall up, but the 'planning' class regresses -> not generalizable
    cases = _cases()
    d = bl.bilevel_decision(
        cases,
        candidate_scores={"p1": 1.0, "p2": 0.1, "v1": 1.0, "v2": 0.1},
        incumbent_scores={"p1": 0.5, "p2": 0.5, "v1": 0.5, "v2": 0.5},
        budget=bl.CostBudget(max_tokens=1000, spent=100),
        max_class_regression=0.05,
    )
    assert d.go is False
    assert "planning" in d.reason


def test_evaluate_from_payload():
    payload = {
        "cases": [
            {"id": "p1", "task_class": "reliability", "split": "public"},
            {"id": "v1", "task_class": "reliability", "split": "private"},
        ],
        "candidate_scores": {"p1": 1.0, "v1": 0.9},
        "incumbent_scores": {"p1": 0.5, "v1": 0.5},
        "budget": {"max_tokens": 1000, "spent": 200},
    }
    out = bl.evaluate(payload)
    assert out["decision"]["go"] is True
    assert out["budget"]["over_budget"] is False


# ---------------------------------------------------------------------------
# McNemar test (#1498)
# ---------------------------------------------------------------------------


def test_mcnemar_no_discordant_pairs():
    """When candidate and incumbent agree on every case, p-value is 1.0."""
    result = bl.mcnemar_test(
        candidate_scores={"a": 1.0, "b": 0.3},
        incumbent_scores={"a": 0.9, "b": 0.4},
    )
    assert result.b == 0
    assert result.c == 0
    assert result.p_value == 1.0
    assert result.significant is False


def test_mcnemar_all_candidate_wins_significant():
    """Every case: candidate passes, incumbent fails → strongly significant."""
    scores = {f"c{i}": 1.0 for i in range(30)}
    inc = {f"c{i}": 0.0 for i in range(30)}
    result = bl.mcnemar_test(scores, inc)
    assert result.b == 30
    assert result.c == 0
    assert result.significant is True
    assert result.p_value < 0.05


def test_mcnemar_balanced_discordant_not_significant():
    """Equal wins for candidate and incumbent → not significant."""
    cand = {f"c{i}": 1.0 if i < 15 else 0.0 for i in range(30)}
    inc = {f"c{i}": 0.0 if i < 15 else 1.0 for i in range(30)}
    result = bl.mcnemar_test(cand, inc)
    assert result.b == 15
    assert result.c == 15
    assert result.significant is False


def test_mcnemar_exact_test_small_sample():
    """Small discordant count uses exact binomial test."""
    # 7 candidate wins, 0 incumbent wins out of 7 discordant
    cand = {"a": 1.0, "b": 1.0, "c": 1.0, "d": 1.0, "e": 1.0, "f": 1.0, "g": 1.0}
    inc = {"a": 0.0, "b": 0.0, "c": 0.0, "d": 0.0, "e": 0.0, "f": 0.0, "g": 0.0}
    result = bl.mcnemar_test(cand, inc)
    assert result.b == 7
    assert result.c == 0
    assert result.significant is True  # p < 0.05 for 7:0 split


def test_bilevel_mcnemar_gate_rejects_insignificant():
    """Private gain exists but McNemar is not significant → reject."""
    # 1 discordant pair out of many tied → mean delta positive but not significant
    cases = [bl.EvalCase(f"c{i}", "cls", bl.Split.private) for i in range(20)]
    # candidate barely beats on 1 case, ties rest — mean delta > 0 but McNemar weak
    cand = {f"c{i}": 0.51 if i == 0 else 0.5 for i in range(20)}
    inc = {f"c{i}": 0.49 if i == 0 else 0.5 for i in range(20)}
    d = bl.bilevel_decision(
        cases,
        candidate_scores=cand,
        incumbent_scores=inc,
        budget=bl.CostBudget(max_tokens=1000, spent=100),
    )
    assert d.go is False
    assert "McNemar not significant" in d.reason
    assert d.mcnemar is not None


def test_bilevel_decision_includes_mcnemar_result():
    """Every decision carries a McNemarResult (even on early-reject paths)."""
    d = bl.bilevel_decision(
        _cases(),
        candidate_scores={"p1": 1.0, "p2": 1.0, "v1": 0.3, "v2": 0.3},
        incumbent_scores={"p1": 0.7, "p2": 0.7, "v1": 0.6, "v2": 0.6},
        budget=bl.CostBudget(max_tokens=1000, spent=100),
    )
    assert d.go is False  # reward hacking
    assert d.mcnemar is not None
    assert "mcnemar" in d.to_dict()


def test_evaluate_payload_includes_mcnemar():
    payload = {
        "cases": [
            {"id": "p1", "task_class": "reliability", "split": "public"},
            {"id": "v1", "task_class": "reliability", "split": "private"},
        ],
        "candidate_scores": {"p1": 1.0, "v1": 1.0},
        "incumbent_scores": {"p1": 0.0, "v1": 0.0},
        "budget": {"max_tokens": 1000, "spent": 200},
    }
    out = bl.evaluate(payload)
    assert out["decision"]["mcnemar"] is not None
    assert out["decision"]["mcnemar"]["b"] == 2
