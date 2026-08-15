"""Unit tests for trajectory-adapted uncertainty quantification (#2386)."""

import pytest

from evolution.lib.uncertainty_quantification import (
    TrajectoryConfidenceAssessment,
    assess_finding_confidence,
    compute_trajectory_equivalence_rate,
    estimate_reflexive_p_true,
)


class TestUncertaintyQuantification:
    """Test uncertainty estimation routines."""

    def test_ter_computation(self):
        # Empty and single
        assert compute_trajectory_equivalence_rate([]) == 0.0
        assert compute_trajectory_equivalence_rate([{"result": "success"}]) == 1.0

        # All identical
        outcomes = [{"status": "ok"}, {"status": "ok"}, {"status": "ok"}]
        assert compute_trajectory_equivalence_rate(outcomes) == 1.0

        # All disjoint
        disjoint = [{"status": "1"}, {"status": "2"}, {"status": "3"}]
        assert compute_trajectory_equivalence_rate(disjoint) == 0.0

        # 2 of 3 matching (pairs: (A,A)=1, (A,B)=0, (A,B)=0 -> 1/3)
        mixed = [{"status": "A"}, {"status": "A"}, {"status": "B"}]
        assert round(compute_trajectory_equivalence_rate(mixed), 2) == 0.33

    def test_estimate_reflexive_p_true(self):
        # High evidence + verified
        solid = {
            "confidence": 80,
            "evidence_pointers": ["file1.py", "file2.py", "issue-123"],
            "verified": True,
            "summary": "Confirmed bug in file operations",
        }
        p_high = estimate_reflexive_p_true(solid)
        assert p_high >= 0.85

        # Uncertain / speculative
        shaky = {
            "confidence": 30,
            "evidence_pointers": [],
            "verified": False,
            "summary": "Maybe this is perhaps related to race condition",
        }
        p_low = estimate_reflexive_p_true(shaky)
        assert p_low <= 0.2

    def test_assess_finding_confidence_actions(self):
        # 1. High confidence finding -> proceed to implementation
        high_finding = {
            "confidence": 85,
            "evidence_pointers": ["logs.txt", "repro.py", "tests.py"],
            "verified": True,
            "summary": "Deterministic failure reproduced with unit test",
        }
        res_high = assess_finding_confidence(high_finding)
        assert res_high.verdict == "high_confidence"
        assert res_high.action == "proceed_to_implementation"
        assert res_high.method == "reflexive_p_true"

        # 2. Medium confidence finding -> second research pass
        med_finding = {
            "confidence": 50,
            "evidence_pointers": ["note.md"],
            "verified": False,
            "summary": "Suspected latency spike under heavy load",
        }
        res_med = assess_finding_confidence(med_finding)
        assert res_med.verdict == "medium_confidence"
        assert res_med.action == "second_research_pass"

        # 3. Low confidence finding -> defer
        low_finding = {
            "confidence": 20,
            "evidence_pointers": [],
            "verified": False,
            "summary": "Unclear and unverified hypothesis",
        }
        res_low = assess_finding_confidence(low_finding)
        assert res_low.verdict == "low_confidence"
        assert res_low.action == "defer"

        # 4. Multi-trajectory hybrid assessment
        trajectories = [
            {"fix": "add_lock", "status": "pass"},
            {"fix": "add_lock", "status": "pass"},
            {"fix": "add_lock", "status": "pass"},
        ]
        res_hybrid = assess_finding_confidence(med_finding, trajectories=trajectories)
        assert res_hybrid.method == "hybrid"
        assert res_hybrid.ter == 1.0
        assert res_hybrid.score >= 75
        assert res_hybrid.action == "proceed_to_implementation"

    def test_assessment_serialization(self):
        assessment = TrajectoryConfidenceAssessment(
            score=82,
            method="hybrid",
            p_true=0.75,
            ter=0.90,
            verdict="high_confidence",
            action="proceed_to_implementation",
            evidence_count=3,
        )
        d = assessment.to_dict()
        assert d["score"] == 82
        assert d["action"] == "proceed_to_implementation"

        loaded = TrajectoryConfidenceAssessment.from_dict(d)
        assert loaded.score == assessment.score
        assert loaded.ter == assessment.ter
        assert loaded.action == assessment.action
