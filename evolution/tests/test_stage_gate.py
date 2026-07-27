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


class TestDecisionLedger:
    """Persisted decisions are what the per-boundary rates are computed from (#1340)."""

    def test_record_and_load_round_trip(self, tmp_path):
        from evolution.lib.stage_gate import load_decisions, record_decision

        ledger = tmp_path / "stage_gate.jsonl"
        record_decision(ledger, decide(_result(90, ["a.json"])))
        record_decision(ledger, decide(_result(50, ["a.json"])))
        recs = load_decisions(ledger)
        assert [r["branch"] for r in recs] == [ACCEPT, REFINE]

    def test_missing_ledger_is_empty(self, tmp_path):
        from evolution.lib.stage_gate import load_decisions

        assert load_decisions(tmp_path / "absent.jsonl") == []

    def test_malformed_lines_skipped(self, tmp_path):
        from evolution.lib.stage_gate import load_decisions, record_decision

        ledger = tmp_path / "stage_gate.jsonl"
        record_decision(ledger, decide(_result(90, ["a.json"])))
        with open(ledger, "a", encoding="utf-8") as fh:
            fh.write("not json\n")
            fh.write('{"branch": "bogus"}\n')
        assert len(load_decisions(ledger)) == 1

    def test_record_never_raises_on_bad_path(self, tmp_path):
        """Instrumentation must not take a live boundary down."""
        from evolution.lib.stage_gate import record_decision

        target = tmp_path / "file"
        target.write_text("x", encoding="utf-8")
        record_decision(target / "nested" / "ledger.jsonl", decide(_result(90)))


class TestGateRates:
    @staticmethod
    def _recs(*branches, stage="local_triage"):
        return [{"branch": b, "stage": stage} for b in branches]

    def test_rates_per_boundary(self):
        from evolution.lib.stage_gate import compute_gate_rates

        rates = compute_gate_rates(self._recs(ACCEPT, ACCEPT, REFINE, RESTART))
        b = rates["local_triage"]
        assert b["total"] == 4
        assert b["stage_refine_rate"] == 0.25
        assert b["stage_restart_rate"] == 0.25

    def test_boundaries_counted_separately(self):
        from evolution.lib.stage_gate import compute_gate_rates

        recs = self._recs(RESTART, RESTART, stage="a") + self._recs(ACCEPT, ACCEPT, stage="b")
        rates = compute_gate_rates(recs)
        assert rates["a"]["stage_restart_rate"] == 1.0
        assert rates["b"]["stage_restart_rate"] == 0.0

    def test_empty_records(self):
        from evolution.lib.stage_gate import compute_gate_rates

        assert compute_gate_rates([]) == {}


class TestGateFlags:
    @staticmethod
    def _recs(*branches, stage="local_triage"):
        return [{"branch": b, "stage": stage} for b in branches]

    def test_high_restart_rate_flagged(self):
        from evolution.lib.stage_gate import compute_gate_rates, gate_flags

        rates = compute_gate_rates(self._recs(RESTART, RESTART, ACCEPT, ACCEPT))
        flags = gate_flags(rates)
        assert len(flags) == 1
        assert flags[0].startswith("HIGH_STAGE_RESTART_RATE:local_triage")

    def test_at_threshold_not_flagged(self):
        """25% is the alert threshold — strictly above it fires."""
        from evolution.lib.stage_gate import compute_gate_rates, gate_flags

        rates = compute_gate_rates(self._recs(RESTART, ACCEPT, ACCEPT, ACCEPT))
        assert rates["local_triage"]["stage_restart_rate"] == 0.25
        assert gate_flags(rates) == []

    def test_small_sample_not_flagged(self):
        """One unlucky restart on a boundary that ran once is not 100% mis-tuned."""
        from evolution.lib.stage_gate import compute_gate_rates, gate_flags

        rates = compute_gate_rates(self._recs(RESTART))
        assert gate_flags(rates) == []

    def test_format_is_empty_without_rates(self):
        from evolution.lib.stage_gate import format_gate_rates

        assert format_gate_rates({}) == ""

    def test_format_names_each_boundary(self):
        from evolution.lib.stage_gate import compute_gate_rates, format_gate_rates

        out = format_gate_rates(compute_gate_rates(self._recs(ACCEPT, REFINE)))
        assert "[stage-gate]" in out
        assert "local_triage" in out
