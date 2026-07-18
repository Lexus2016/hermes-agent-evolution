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
    pub, priv = bl.partition_scores(_cases(), {"p1": 1.0, "v1": 0.5, "p2": 0.8, "v2": 0.4})
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
        candidate_scores={"p1": 1.0, "p2": 1.0, "v1": 0.3, "v2": 0.3},  # public up, private down
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
