"""Tests for continuous partial-credit grading (#1302)."""

from scripts.evolution_partial_credit import grade_partial_credit


def test_solved_grade():
    grade = grade_partial_credit(10, 10, 1.0)
    assert grade.score == 1.0
    assert grade.solved is True
    assert grade.band == "solved"
    assert grade.progress_made is False


def test_partial_credit_high_unsolved():
    grade = grade_partial_credit(8, 10, 1.0)
    assert grade.score == 0.8
    assert grade.solved is False
    assert grade.band == "unsolved_high"
    assert grade.progress_made is True


def test_partial_credit_low_unsolved():
    grade = grade_partial_credit(1, 10, 1.0)
    assert grade.score == 0.1
    assert grade.solved is False
    assert grade.band == "unsolved_low"
    assert grade.progress_made is True
