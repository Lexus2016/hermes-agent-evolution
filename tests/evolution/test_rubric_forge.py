# -*- coding: utf-8 -*-
"""Unit tests for RubricForge Slice 1 — rubric-evolution primitive (#2780)."""

from evolution.lib.rubric_forge import RubricScore, score_rubric, select_best_rubric


def _judge(rubric: str, example: str) -> bool:
    """A judge that passes when the rubric mentions the example's keyword."""
    return example in rubric


def test_scores_perfect_agreement():
    score = score_rubric("must handle alpha and beta", ["alpha", "beta"], [True, True], _judge)
    assert score.agreement == 1.0
    assert score.correct == 2 and score.total == 2
    assert score.per_example == {"0": True, "1": True}


def test_scores_partial_agreement():
    score = score_rubric("must handle alpha", ["alpha", "beta"], [True, True], _judge)
    assert score.agreement == 0.5
    assert score.correct == 1 and score.total == 2
    assert score.per_example == {"0": True, "1": False}


def test_mismatched_label_is_counted():
    score = score_rubric("must handle alpha", ["alpha"], [False], _judge)
    assert score.agreement == 0.0
    assert score.correct == 0


def test_empty_labeled_set_is_zero_not_crash():
    score = score_rubric("x", [], [], _judge)
    assert score.agreement == 0.0 and score.total == 0


def test_unequal_sequences_raise():
    try:
        score_rubric("x", ["a"], [True, False], _judge)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_judge_that_raises_is_a_mismatch():
    def flaky_judge(rubric: str, example: str) -> bool:
        if example == "bad":
            raise RuntimeError("nope")
        return True

    score = score_rubric("x", ["good", "bad"], [True, True], flaky_judge)
    assert score.agreement == 0.5
    assert score.per_example == {"0": True, "1": False}


def test_select_best_rubric_picks_highest_agreement():
    best = select_best_rubric(
        ["must handle alpha", "must handle alpha and beta"],
        ["alpha", "beta"],
        [True, True],
        _judge,
    )
    assert best.rubric == "must handle alpha and beta"
    assert best.agreement == 1.0


def test_select_best_ties_break_to_first():
    best = select_best_rubric(
        ["must handle alpha", "must handle alpha"],
        ["alpha"],
        [True],
        _judge,
    )
    assert best.rubric == "must handle alpha"


def test_select_best_empty_candidates():
    best = select_best_rubric([], ["alpha"], [True], _judge)
    assert best.rubric == "" and best.agreement == 0.0


def test_score_is_a_dataclass():
    s = RubricScore(rubric="r", agreement=0.5, correct=1, total=2)
    assert s.per_example == {}
