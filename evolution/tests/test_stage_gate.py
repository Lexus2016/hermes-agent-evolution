# -*- coding: utf-8 -*-
"""Tests for the confidence gate at stage boundaries (issue #1339)."""

from __future__ import annotations

from evolution.lib.stage_gate import (
    ACCEPT,
    DEFAULT_CONFIDENCE_THRESHOLD,
    REFINE,
    RESTART,
    GateDecision,
    decide,
)
from evolution.lib.stage_result import StageResult


def _result(confidence: int, evidence: list[str] | None = None) -> StageResult:
    return StageResult(
        result={"payload": True},
        evidence_pointers=list(evidence or []),
        confidence=confidence,
        stage="local_triage",
        timestamp="2026-07-27T00:00:00+00:00",
    )


class TestAcceptBranch:
    def test_at_threshold_accepts(self):
        d = decide(_result(DEFAULT_CONFIDENCE_THRESHOLD, ["a.json"]))
        assert d.branch == ACCEPT
        assert d.proceeds is True

    def test_above_threshold_accepts(self):
        assert decide(_result(95, ["a.json"])).branch == ACCEPT

    def test_accept_without_evidence_still_accepts(self):
        """High confidence is sufficient on its own — evidence is not required."""
        assert decide(_result(90)).branch == ACCEPT

    def test_accept_retains_evidence(self):
        d = decide(_result(90, ["a.json", "b.json"]))
        assert d.retained_evidence == ["a.json", "b.json"]


class TestRefineBranch:
    def test_below_threshold_with_evidence_refines(self):
        d = decide(_result(50, ["a.json"]))
        assert d.branch == REFINE
        assert d.proceeds is False

    def test_refine_carries_evidence_forward(self):
        """Refine preserves reliable findings rather than discarding them."""
        d = decide(_result(50, ["a.json", "b.json"]))
        assert d.retained_evidence == ["a.json", "b.json"]

    def test_wrap_default_confidence_lands_in_refine(self):
        """StageResult.wrap assigns 50 when evidence exists but confidence is
        unset. That must NOT be silently accepted — an un-assessed stage is not
        a confident one."""
        sr = StageResult.wrap(result={}, evidence_pointers=["a.json"], stage="s")
        assert sr.confidence == 50
        assert decide(sr).branch == REFINE

    def test_explicit_recoverable_overrides_absent_evidence(self):
        """A model judging the trajectory recoverable wins over the proxy."""
        d = decide(_result(20), recoverable=True)
        assert d.branch == REFINE


class TestRestartBranch:
    def test_below_threshold_without_evidence_restarts(self):
        d = decide(_result(10))
        assert d.branch == RESTART
        assert d.proceeds is False

    def test_restart_discards_evidence(self):
        d = decide(_result(10))
        assert d.retained_evidence == []

    def test_explicit_not_recoverable_overrides_present_evidence(self):
        """A model judging the trajectory too noisy wins over the proxy."""
        d = decide(_result(20, ["a.json"]), recoverable=False)
        assert d.branch == RESTART
        assert d.retained_evidence == []


class TestThresholdHandling:
    def test_custom_threshold_respected(self):
        assert decide(_result(60, ["a.json"]), threshold=50).branch == ACCEPT
        assert decide(_result(60, ["a.json"]), threshold=80).branch == REFINE

    def test_threshold_clamped(self):
        assert decide(_result(100, ["a.json"]), threshold=500).branch == ACCEPT
        assert decide(_result(0), threshold=-10).branch == ACCEPT

    def test_confidence_clamped(self):
        sr = StageResult(confidence=1000, stage="s", evidence_pointers=["a"])
        assert decide(sr).confidence == 100

    def test_default_threshold_is_conservative(self):
        """A conservative tau is what stops runaway looping (#1339)."""
        assert DEFAULT_CONFIDENCE_THRESHOLD == 70


class TestDecisionRecord:
    def test_to_dict_round_trip(self):
        d = decide(_result(50, ["a.json"]))
        as_dict = d.to_dict()
        assert as_dict["branch"] == REFINE
        assert as_dict["stage"] == "local_triage"
        assert as_dict["retained_evidence"] == ["a.json"]

    def test_log_line_names_stage_and_branch(self):
        line = decide(_result(50, ["a.json"])).log_line()
        assert "[stage-gate]" in line
        assert "local_triage" in line
        assert "REFINE" in line

    def test_unknown_stage_label(self):
        assert decide(StageResult(confidence=90)).stage == "unknown"

    def test_is_a_gate_decision(self):
        assert isinstance(decide(_result(90)), GateDecision)
