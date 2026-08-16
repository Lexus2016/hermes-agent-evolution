# -*- coding: utf-8 -*-
"""Unit tests for parallel batch tool evolution + transfer gate (#2260)."""

from evolution.lib.tool_batch_evolution import (
    VariantResult,
    accept_best,
    propose_variants,
    run_batch,
    select_best,
    transferability_score,
)
from evolution.lib.tool_synthesis import SynthesizedTool, ToolProposer


def _base_tool() -> SynthesizedTool:
    return ToolProposer.propose("parse a CSV file", "csv_parser")


def _variant(name: str) -> SynthesizedTool:
    return SynthesizedTool(name=name, description=f"desc {name}", code="def f(): pass")


def _result(name: str, passed: bool, score: float) -> VariantResult:
    return VariantResult(variant=_variant(name), passed=passed, score=score)


def _results(specs) -> list:
    """Build results from (name, passed, score) tuples."""
    return [_result(n, p, s) for n, p, s in specs]


class TestProposeVariants:
    def test_produces_n_distinct_variants(self):
        variants = propose_variants(_base_tool(), 5)
        assert len(variants) == 5
        assert len({v.description for v in variants}) == 5

    def test_more_variants_than_hints_stay_distinct(self):
        variants = propose_variants(_base_tool(), 10)
        assert len({v.description for v in variants}) == 10

    def test_variants_preserve_base_identity(self):
        base = _base_tool()
        variants = propose_variants(base, 3)
        assert all(v.name == base.name for v in variants)
        assert all(v.accepted is False for v in variants)

    def test_default_mutation_keeps_code_consistent_with_description(self):
        variants = propose_variants(_base_tool(), 1)
        assert variants[0].description in variants[0].code

    def test_injected_proposer_is_used(self):
        def proposer(base: SynthesizedTool, i: int) -> SynthesizedTool:
            return SynthesizedTool(name=f"v{i}", description="d", code="c")

        variants = propose_variants(_base_tool(), 3, proposer=proposer)
        assert [v.name for v in variants] == ["v0", "v1", "v2"]


class TestRunBatch:
    def test_collects_all_outcomes(self):
        variants = [_variant(f"v{i}") for i in range(4)]
        scores = {"v0": 1.0, "v1": 0.0, "v2": 0.75, "v3": False}
        results = run_batch(variants, validator=lambda t: scores[t.name], max_workers=2)
        by_name = {r.variant.name: r for r in results}
        assert len(results) == 4
        assert by_name["v0"].passed is True and by_name["v0"].score == 1.0
        assert by_name["v1"].passed is False
        assert by_name["v2"].passed is True and by_name["v2"].score == 0.75
        assert by_name["v3"].passed is False and by_name["v3"].score == 0.0

    def test_validator_sees_every_variant_exactly_once(self):
        variants = [_variant(f"v{i}") for i in range(3)]
        seen: list = []

        def validator(t: SynthesizedTool) -> float:
            seen.append(t.name)
            return 1.0

        results = run_batch(variants, validator=validator, max_workers=3)
        assert sorted(seen) == ["v0", "v1", "v2"]
        assert len(results) == 3

    def test_empty_batch_returns_empty(self):
        assert run_batch([], validator=lambda t: 1.0) == []


class TestSelectBest:
    def test_picks_max_score_passer(self):
        results = _results([("a", True, 0.5), ("b", True, 0.9), ("c", True, 0.7)])
        assert select_best(results).name == "b"

    def test_ignores_failing_even_if_higher_score(self):
        results = _results([("a", True, 0.5), ("b", False, 0.99)])
        assert select_best(results).name == "a"

    def test_returns_none_when_nothing_passes(self):
        results = _results([("a", False, 0.0), ("b", False, 0.1)])
        assert select_best(results) is None

    def test_empty_results_return_none(self):
        assert select_best([]) is None


class TestTransferabilityScore:
    def test_equal_pass_rates_score_one(self):
        origin = _results([("a", True, 1.0), ("b", True, 1.0), ("c", False, 0.0), ("d", False, 0.0)])
        held = _results([("a", True, 1.0), ("b", False, 0.0), ("c", True, 1.0), ("d", False, 0.0)])
        assert transferability_score(origin, held) == 1.0

    def test_partial_transfer_ratio(self):
        origin = _results([("a", True, 1.0), ("b", True, 1.0), ("c", True, 1.0), ("d", True, 1.0)])
        held = _results([("a", True, 1.0), ("b", False, 0.0), ("c", False, 0.0), ("d", False, 0.0)])
        assert transferability_score(origin, held) == 0.25

    def test_zero_origin_rate_is_zero_even_if_held_out_passes(self):
        origin = _results([("a", False, 0.0), ("b", False, 0.0)])
        held = _results([("a", True, 1.0), ("b", True, 1.0)])
        assert transferability_score(origin, held) == 0.0

    def test_empty_held_out_is_zero(self):
        origin = _results([("a", True, 1.0)])
        assert transferability_score(origin, []) == 0.0


class TestAcceptBest:
    def test_rejects_overfit_variant(self):
        origin = _results([("a", True, 1.0), ("b", True, 0.8)])
        held = _results([(f"h{i}", i < 3, 1.0 if i < 3 else 0.0) for i in range(10)])
        assert accept_best(origin, held) is None

    def test_accepts_transferable_variant_and_returns_best(self):
        origin = _results([("a", True, 0.9), ("b", True, 0.6), ("c", True, 0.7), ("d", False, 0.0), ("e", False, 0.0)])
        held = _results([("a", True, 1.0), ("b", False, 0.0), ("c", True, 1.0), ("d", True, 1.0), ("e", False, 0.0)])
        best = accept_best(origin, held)
        assert best is not None
        assert best.name == "a"

    def test_rejects_when_no_origin_variant_passes(self):
        origin = _results([("a", False, 0.0), ("b", False, 0.0)])
        held = _results([("a", True, 1.0), ("b", True, 1.0)])
        assert accept_best(origin, held) is None

    def test_threshold_boundary_is_inclusive(self):
        origin = _results([("a", True, 1.0), ("b", True, 1.0)])
        held = _results([("a", True, 1.0), ("b", False, 0.0)])
        # transferability == 0.5 exactly
        assert accept_best(origin, held, threshold=0.5) is not None
        assert accept_best(origin, held, threshold=0.6) is None


class TestEndToEnd:
    def test_propose_run_select_gate_pipeline(self):
        variants = propose_variants(_base_tool(), 4)

        call_count = {"n": 0}

        def validator(t: SynthesizedTool) -> float:
            call_count["n"] += 1
            return [1.0, 0.5, 0.0, 0.75][call_count["n"] - 1]

        results = run_batch(variants, validator=validator, max_workers=4)
        best = select_best(results)
        assert best is not None
        assert best.accepted is False
        assert accept_best(results, results) is not None
