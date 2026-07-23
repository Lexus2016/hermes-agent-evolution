"""Tests for the MAS-FIRE coordination fault-injection harness (#1211)."""

import pytest

from scripts.evolution_mas_fire_harness import (
    FaultInjector,
    FaultResult,
    FaultToleranceScorer,
    FaultToleranceTier,
    FaultType,
    run_fault_injection_suite,
    summarize_results,
)


class TestFaultType:
    """Fault type constants are complete and non-empty."""

    def test_all_fault_types_defined(self):
        assert len(FaultType.ALL) == 6
        assert FaultType.STALE_SHARED_STATE in FaultType.ALL
        assert FaultType.CORRUPTED_SUMMARY in FaultType.ALL
        assert FaultType.TRUNCATED_SUMMARY in FaultType.ALL
        assert FaultType.INSTRUCTION_MISINTERPRETATION in FaultType.ALL
        assert FaultType.REASONING_DRIFT in FaultType.ALL
        assert FaultType.LOST_UPDATE in FaultType.ALL

    def test_all_fault_types_are_strings(self):
        for ft in FaultType.ALL:
            assert isinstance(ft, str)
            assert len(ft) > 0


class TestFaultToleranceTier:
    """Tier enum ordering and labels."""

    def test_tier_ordering(self):
        assert FaultToleranceTier.NONE < FaultToleranceTier.MECHANISM
        assert FaultToleranceTier.MECHANISM < FaultToleranceTier.RULE
        assert FaultToleranceTier.RULE < FaultToleranceTier.PROMPT
        assert FaultToleranceTier.PROMPT < FaultToleranceTier.REASONING

    def test_tier_labels(self):
        assert FaultToleranceTier.NONE.label == "none"
        assert FaultToleranceTier.MECHANISM.label == "mechanism"
        assert FaultToleranceTier.RULE.label == "rule"
        assert FaultToleranceTier.PROMPT.label == "prompt"
        assert FaultToleranceTier.REASONING.label == "reasoning"


class TestFaultInjector:
    """Each injection mechanism produces the expected fault."""

    def test_corrupted_summary_changes_content(self):
        clean = "The analysis is complete with 3 findings."
        corrupted = FaultInjector.inject_corrupted_summary("task", clean, 0.3)
        assert corrupted != clean
        # Some characters should be different
        diffs = sum(1 for a, b in zip(clean, corrupted) if a != b)
        assert diffs > 0

    def test_truncated_summary_shortens(self):
        clean = "This is a long summary that should be truncated to test the harness."
        truncated = FaultInjector.inject_truncated_summary(clean, 0.3)
        assert len(truncated) < len(clean)
        assert "[truncated]" in truncated

    def test_stale_shared_state_detects_divergence(self):
        result = FaultInjector.inject_stale_shared_state("v1", "v2")
        assert result["divergence"] is True
        assert result["subagent_a_stale_read"] == "v1"
        assert result["subagent_b_current_write"] == "v2"

    def test_stale_shared_state_no_divergence(self):
        result = FaultInjector.inject_stale_shared_state("same", "same")
        assert result["divergence"] is False

    def test_instruction_misinterpretation_shows_divergence(self):
        result = FaultInjector.inject_instruction_misinterpretation(
            "Sort by size", "Sort by name"
        )
        assert result["divergence"] is True
        assert result["original_instruction"] == "Sort by size"
        assert result["subagent_interpretation"] == "Sort by name"

    def test_reasoning_drift_detected(self):
        result = FaultInjector.inject_reasoning_drift(
            "analyze security", "I wrote a poem about cats"
        )
        assert result["drifted"] is True

    def test_reasoning_drift_not_detected_when_aligned(self):
        result = FaultInjector.inject_reasoning_drift(
            "analyze security", "I will analyze security now"
        )
        assert result["drifted"] is False

    def test_lost_update_shows_overwrite(self):
        result = FaultInjector.inject_lost_update("data=v1", "data=v2")
        assert result["update_lost"] is True
        assert result["subagent_a_update"] == "data=v1"
        assert result["subagent_b_overwrite"] == "data=v2"


class TestFaultToleranceScorer:
    """Scoring classifies parent responses into the correct tier."""

    def setUp(self):
        self.scorer = FaultToleranceScorer()

    def test_no_detection_silent_propagation(self):
        scorer = FaultToleranceScorer()
        result = scorer.score(
            FaultType.CORRUPTED_SUMMARY,
            "garbled output here",
            "Here is the result based on the subagent's work.",
        )
        assert result.detected is False
        assert result.tier == FaultToleranceTier.NONE
        assert len(result.gap_description) > 0

    def test_mechanism_detection(self):
        scorer = FaultToleranceScorer()
        result = scorer.score(
            FaultType.CORRUPTED_SUMMARY,
            "garbled",
            "The subagent output caused a parse error. Retrying.",
        )
        assert result.detected is True
        assert result.tier >= FaultToleranceTier.MECHANISM

    def test_rule_detection(self):
        scorer = FaultToleranceScorer()
        result = scorer.score(
            FaultType.TRUNCATED_SUMMARY,
            "Task comp...[truncated]",
            "The summary appears truncated and does not match the expected schema.",
        )
        assert result.detected is True
        assert result.tier >= FaultToleranceTier.RULE

    def test_reasoning_detection(self):
        scorer = FaultToleranceScorer()
        result = scorer.score(
            FaultType.REASONING_DRIFT,
            "I wrote a poem about cats",
            "I notice the output seems inconsistent with the task goal. This is suspicious.",
        )
        assert result.detected is True
        assert result.tier >= FaultToleranceTier.REASONING

    def test_highest_tier_wins(self):
        scorer = FaultToleranceScorer()
        # Response has both reasoning and mechanism signals
        result = scorer.score(
            FaultType.CORRUPTED_SUMMARY,
            "garbled",
            "I notice the output seems suspicious. There was also a parse error.",
        )
        assert result.tier == FaultToleranceTier.REASONING


class TestRunFaultInjectionSuite:
    """The full suite exercises all 6 fault types."""

    def test_suite_returns_6_results(self):
        # Mock parent that detects nothing
        def blind_parent(fault_type, faulty_output):
            return f"Here is the result: {faulty_output[:20]}"

        results = run_fault_injection_suite(blind_parent)
        assert len(results) == 6
        fault_types = {r.fault_type for r in results}
        assert fault_types == set(FaultType.ALL)

    def test_suite_with_detecting_parent(self):
        # Mock parent that always detects via reasoning
        def detecting_parent(fault_type, faulty_output):
            return f"I notice the output appears suspicious and inconsistent."

        results = run_fault_injection_suite(detecting_parent)
        assert all(r.detected for r in results)
        assert all(r.tier >= FaultToleranceTier.REASONING for r in results)


class TestSummarizeResults:
    """Summary report includes detection rate, tiers, and gaps."""

    def test_summary_with_no_detections(self):
        def blind_parent(fault_type, faulty_output):
            return f"Result: {faulty_output[:15]}"

        results = run_fault_injection_suite(blind_parent)
        summary = summarize_results(results)
        assert summary["total_faults"] == 6
        assert summary["detected"] == 0
        assert summary["detection_rate"] == 0.0
        assert len(summary["gaps"]) == 6
        assert summary["average_tier"] == 0.0

    def test_summary_with_all_detections(self):
        def detecting_parent(fault_type, faulty_output):
            return "I notice this appears suspicious and inconsistent."

        results = run_fault_injection_suite(detecting_parent)
        summary = summarize_results(results)
        assert summary["detected"] == 6
        assert summary["detection_rate"] == 1.0
        assert len(summary["gaps"]) == 0
        assert summary["average_tier"] == 4.0  # all REASONING

    def test_summary_per_fault_structure(self):
        def blind_parent(fault_type, faulty_output):
            return "ok"

        results = run_fault_injection_suite(blind_parent)
        summary = summarize_results(results)
        assert len(summary["per_fault"]) == 6
        for pf in summary["per_fault"]:
            assert "fault_type" in pf
            assert "tier" in pf
            assert "detected" in pf
            assert "evidence" in pf

    def test_empty_results(self):
        summary = summarize_results([])
        assert summary["total_faults"] == 0
        assert summary["detection_rate"] == 0.0
        assert summary["gaps"] == []
