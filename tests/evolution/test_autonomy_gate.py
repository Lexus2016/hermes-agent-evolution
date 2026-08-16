# -*- coding: utf-8 -*-
"""Unit tests for the autonomy gate + red-line config (#2528)."""

from evolution.lib.autonomy_gate import (
    AutonomyVerdict,
    RedLineVerdict,
    check_autonomy_gate,
    check_red_line_config,
)


class TestAutonomyGate:
    def test_non_high_risk_action_allowed(self):
        verdict = check_autonomy_gate("read_file")
        assert verdict.allowed is True
        assert verdict.verification_required is False

    def test_high_risk_unverified_blocked(self):
        verdict = check_autonomy_gate("send_message")
        assert verdict.allowed is False
        assert "not verified" in verdict.reason

    def test_high_risk_verified_allowed(self):
        verdict = check_autonomy_gate(
            "send_message", verified=True, verification_evidence="dry-run passed"
        )
        assert verdict.allowed is True
        assert "dry-run passed" in verdict.reason

    def test_high_risk_verified_no_evidence_blocked(self):
        verdict = check_autonomy_gate(
            "git_push", verified=True, verification_evidence=""
        )
        assert verdict.allowed is False
        assert "no verification evidence" in verdict.reason

    def test_high_risk_human_approved_allowed(self):
        verdict = check_autonomy_gate("delete_file", human_approved=True)
        assert verdict.allowed is True
        assert "Human approved" in verdict.reason

    def test_verdict_serialization(self):
        v = AutonomyVerdict(action="send_message", allowed=False, reason="r")
        d = v.to_dict()
        restored = AutonomyVerdict.from_dict(d)
        assert restored.action == "send_message"
        assert restored.allowed is False


class TestRedLineConfig:
    def test_red_line_when_both_conditions(self):
        verdict = check_red_line_config(safeguards_disabled=True, internet_enabled=True)
        assert verdict.is_red_line is True
        assert "RED-LINE" in verdict.reason

    def test_not_red_line_when_safeguards_on(self):
        verdict = check_red_line_config(
            safeguards_disabled=False, internet_enabled=True
        )
        assert verdict.is_red_line is False

    def test_not_red_line_when_no_internet(self):
        verdict = check_red_line_config(
            safeguards_disabled=True, internet_enabled=False
        )
        assert verdict.is_red_line is False

    def test_not_red_line_when_neither(self):
        verdict = check_red_line_config()
        assert verdict.is_red_line is False

    def test_verdict_serialization(self):
        v = RedLineVerdict(
            is_red_line=True,
            safeguards_disabled=True,
            internet_enabled=True,
            reason="r",
        )
        d = v.to_dict()
        restored = RedLineVerdict.from_dict(d)
        assert restored.is_red_line is True
        assert restored.safeguards_disabled is True
