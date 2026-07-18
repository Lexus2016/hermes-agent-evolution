# -*- coding: utf-8 -*-
"""Tests for scripts/evolution_context_quality.py (#1163)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import evolution_context_quality as cq  # noqa: E402


def _strong_ctx():
    return {
        "role": " ".join(["word"] * 40),
        "guardrails": ["g1", "g2", "g3", "g4", "g5"],
        "instructions": ["do a", "do b"],
        "instruction_conflicts": 0,
        "tool_schemas": [
            {"function": {"description": "reads a file", "parameters": {"type": "object"}}},
        ],
        "grounding": ["src1", "src2", "src3"],
        "untrusted_input_handling": True,
        "token_count": 1000,
        "token_budget": 8000,
    }


def test_score_context_returns_seven_criteria():
    report = cq.score_context(_strong_ctx())
    assert len(report.scores) == 7
    assert set(report.by_criterion().keys()) == set(cq.Criterion)


def test_strong_context_scores_high():
    report = cq.score_context(_strong_ctx())
    assert report.combined >= 0.8


def test_empty_context_scores_low():
    report = cq.score_context({})
    assert report.combined < 0.5


def test_missing_signals_do_not_error():
    # a partial context must score, not raise
    report = cq.score_context({"role": "helper"})
    assert isinstance(report.combined, float)


def test_guardrail_regression_is_flagged():
    before = _strong_ctx()
    after = dict(before)
    after["guardrails"] = []  # drop all guardrails
    cmp = cq.compare_context(before, after)
    assert cmp["blocked"] is True
    flagged = {f["criterion"] for f in cmp["regressed"]}
    assert "guardrail_coverage" in flagged


def test_no_regression_not_blocked():
    ctx = _strong_ctx()
    cmp = cq.compare_context(ctx, dict(ctx))
    assert cmp["blocked"] is False
    assert cmp["regressed"] == []


def test_tool_schema_quality_rewards_full_description():
    good = cq.score_context({"tool_schemas": [{"function": {"description": "x", "parameters": {}}}]})
    bad = cq.score_context({"tool_schemas": [{"function": {"description": "", "parameters": None}}]})
    assert good.by_criterion()[cq.Criterion.tool_schema_quality] > bad.by_criterion()[cq.Criterion.tool_schema_quality]


def test_token_efficiency_penalizes_over_budget():
    under = cq.score_context({"token_count": 100, "token_budget": 1000})
    over = cq.score_context({"token_count": 2000, "token_budget": 1000})
    assert under.by_criterion()[cq.Criterion.token_efficiency] > over.by_criterion()[cq.Criterion.token_efficiency]


def test_evaluate_without_before_has_no_comparison():
    result = cq.evaluate(None, _strong_ctx())
    assert "report" in result
    assert "comparison" not in result


def test_evaluate_with_before_has_comparison():
    ctx = _strong_ctx()
    worse = dict(ctx)
    worse["guardrails"] = []
    result = cq.evaluate(ctx, worse)
    assert result["comparison"]["blocked"] is True
