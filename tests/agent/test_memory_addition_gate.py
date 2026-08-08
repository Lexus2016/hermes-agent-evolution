"""Tests for selective memory addition gate (#1270)."""

from agent.memory_addition_gate import evaluate_memory_addition


def test_high_quality_memory_accepted():
    decision = evaluate_memory_addition("Key lesson: always pin dependencies", eval_score=0.9)
    assert decision.allow_addition is True
    assert decision.score >= 0.6


def test_noisy_memory_rejected():
    decision = evaluate_memory_addition("Error: Traceback (most recent call last)", eval_score=0.8)
    assert decision.allow_addition is False
    assert decision.score < 0.6


def test_empty_memory_rejected():
    decision = evaluate_memory_addition("")
    assert decision.allow_addition is False
    assert decision.score == 0.0
