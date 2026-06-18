"""Tests for the category-aware judge aggregator (issue #318).

A pure, additive layer over ``agent.agent_judge``: it groups a list of
``JudgeVerdict``s by a benchmark-task ``category`` label into per-category pass
rates and surfaces the weakest categories as evolution targets.  No LLM, no
rubric change, no mutation of ``AgentJudge.score`` behaviour.
"""

import json

import pytest

from agent.agent_judge import JudgeVerdict, TraceSummary
from agent.judge_categories import (
    CategoryStats,
    aggregate_by_category,
    weakest_categories,
)


def _verdict(session_id: str, overall: float) -> JudgeVerdict:
    """A minimal verdict at a chosen overall score (rest is irrelevant here)."""
    return JudgeVerdict(
        session_id=session_id,
        overall_score=overall,
        dimension_scores={},
        rationale="",
        method="heuristic",
        trace_summary=TraceSummary(),
    )


# A small labelled batch spanning three categories with mixed outcomes.
#   recognition:    2 pass, 0 fail  -> 1.00
#   business-logic: 1 pass, 1 fail  -> 0.50
#   race:           0 pass, 2 fail  -> 0.00  (a zero-pass category)
SAMPLE_BATCH = [
    ("recognition", _verdict("r1", 0.90)),
    ("recognition", _verdict("r2", 0.75)),
    ("business-logic", _verdict("b1", 0.80)),
    ("business-logic", _verdict("b2", 0.40)),
    ("race", _verdict("c1", 0.30)),
    ("race", _verdict("c2", 0.10)),
]


class TestAggregateByCategory:
    def test_empty_batch_yields_empty_mapping(self):
        assert aggregate_by_category([]) == {}

    def test_groups_and_computes_pass_rate(self):
        agg = aggregate_by_category(SAMPLE_BATCH)
        assert set(agg) == {"recognition", "business-logic", "race"}
        assert agg["recognition"].pass_rate == 1.0
        assert agg["recognition"].n == 2
        assert agg["recognition"].passed == 2
        assert agg["business-logic"].pass_rate == 0.5
        assert agg["business-logic"].passed == 1
        assert agg["race"].pass_rate == 0.0
        assert agg["race"].passed == 0
        assert agg["race"].n == 2

    def test_threshold_is_respected(self):
        # At a 0.85 threshold only the 0.90 verdict passes recognition.
        agg = aggregate_by_category(SAMPLE_BATCH, threshold=0.85)
        assert agg["recognition"].passed == 1
        assert agg["recognition"].pass_rate == 0.5
        # business-logic and race now have zero passes.
        assert agg["business-logic"].passed == 0
        assert agg["race"].passed == 0

    def test_uses_verdict_passed_semantics(self):
        # A verdict exactly at the threshold passes (>= in JudgeVerdict.passed).
        batch = [("edge", _verdict("e1", 0.7))]
        agg = aggregate_by_category(batch, threshold=0.7)
        assert agg["edge"].passed == 1
        assert agg["edge"].pass_rate == 1.0

    def test_single_category(self):
        batch = [("solo", _verdict("s1", 0.9)), ("solo", _verdict("s2", 0.2))]
        agg = aggregate_by_category(batch)
        assert list(agg) == ["solo"]
        assert agg["solo"].n == 2
        assert agg["solo"].passed == 1
        assert agg["solo"].pass_rate == 0.5

    def test_category_label_whitespace_normalised(self):
        # Leading/trailing whitespace in a label should not fork a category.
        batch = [("race", _verdict("c1", 0.1)), ("  race  ", _verdict("c2", 0.2))]
        agg = aggregate_by_category(batch)
        assert set(agg) == {"race"}
        assert agg["race"].n == 2

    def test_missing_category_label_rejected(self):
        with pytest.raises(ValueError):
            aggregate_by_category([("", _verdict("x", 0.5))])

    def test_stats_to_dict_is_json_serialisable(self):
        agg = aggregate_by_category(SAMPLE_BATCH)
        payload = {cat: stats.to_dict() for cat, stats in agg.items()}
        # Must round-trip through JSON for the evolution report.
        json.dumps(payload)
        rec = payload["recognition"]
        assert rec == {"pass_rate": 1.0, "n": 2, "passed": 2}


class TestCategoryStats:
    def test_pass_rate_derived_from_passed_and_n(self):
        stats = CategoryStats(category="x", n=4, passed=1)
        assert stats.pass_rate == 0.25

    def test_zero_n_pass_rate_is_zero_not_division_error(self):
        stats = CategoryStats(category="x", n=0, passed=0)
        assert stats.pass_rate == 0.0


class TestWeakestCategories:
    def test_returns_lowest_pass_rates_first(self):
        agg = aggregate_by_category(SAMPLE_BATCH)
        weakest = weakest_categories(agg)
        cats = [w.category for w in weakest]
        # race (0.0) before business-logic (0.5) before recognition (1.0).
        assert cats[0] == "race"
        assert cats.index("business-logic") < cats.index("recognition")

    def test_max_pass_rate_filter_surfaces_only_weak(self):
        agg = aggregate_by_category(SAMPLE_BATCH)
        # Only categories at or below 0.5 pass rate are evolution targets.
        weak = weakest_categories(agg, max_pass_rate=0.5)
        cats = {w.category for w in weak}
        assert cats == {"race", "business-logic"}
        assert "recognition" not in cats

    def test_zero_pass_category_always_surfaced(self):
        agg = aggregate_by_category(SAMPLE_BATCH)
        weak = weakest_categories(agg, max_pass_rate=0.0)
        # Success criterion: a category with zero passes triggers a target.
        assert [w.category for w in weak] == ["race"]

    def test_limit_caps_result_count(self):
        agg = aggregate_by_category(SAMPLE_BATCH)
        weak = weakest_categories(agg, limit=1)
        assert len(weak) == 1
        assert weak[0].category == "race"

    def test_empty_aggregate_yields_no_targets(self):
        assert weakest_categories({}) == []

    def test_deterministic_tie_break_by_category_name(self):
        # Two categories with identical pass rate sort by name (stable re-runs).
        batch = [
            ("zeta", _verdict("z1", 0.1)),
            ("alpha", _verdict("a1", 0.1)),
        ]
        agg = aggregate_by_category(batch)
        weak = weakest_categories(agg)
        assert [w.category for w in weak] == ["alpha", "zeta"]


class TestEvolutionReportShape:
    def test_report_dict_has_per_category_and_weakest(self):
        from agent.judge_categories import build_category_report

        report = build_category_report(SAMPLE_BATCH, threshold=0.7)
        json.dumps(report)  # JSON-serialisable for the evolution report
        assert "per_category" in report
        assert "weakest_categories" in report
        assert report["per_category"]["race"]["pass_rate"] == 0.0
        # The zero-pass category is an evolution target.
        assert "race" in report["weakest_categories"]

    def test_empty_report(self):
        from agent.judge_categories import build_category_report

        report = build_category_report([])
        assert report == {"per_category": {}, "weakest_categories": []}
