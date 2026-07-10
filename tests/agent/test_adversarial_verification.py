"""Tests for agent.adversarial_verification (#825)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from agent.adversarial_verification import (
    VerificationIssue,
    VerificationResult,
    VerificationSeverity,
    VerificationVerdict,
    _clamp_confidence,
    _extract_json,
    generate_verifier_prompt,
    parse_verifier_output,
    verify_adversarial,
)


class TestEnumParsing:
    def test_severity_round_trip(self):
        for m in VerificationSeverity:
            assert VerificationSeverity.from_str(m.name) is m

    def test_severity_unknown_defaults_major(self):
        assert VerificationSeverity.from_str("nonsense") is VerificationSeverity.MAJOR

    def test_verdict_round_trip(self):
        for m in VerificationVerdict:
            assert VerificationVerdict.from_str(m.name) is m

    def test_verdict_with_spaces(self):
        assert (
            VerificationVerdict.from_str("approved with changes")
            is VerificationVerdict.APPROVED_WITH_CHANGES
        )

    def test_verdict_unknown_defaults_changes(self):
        assert (
            VerificationVerdict.from_str("maybe")
            is VerificationVerdict.APPROVED_WITH_CHANGES
        )


class TestVerificationResult:
    def test_passed_and_blocker(self):
        assert VerificationResult(verdict=VerificationVerdict.APPROVED).passed
        assert not VerificationResult(verdict=VerificationVerdict.REJECTED).passed
        blocked = VerificationResult(
            verdict=VerificationVerdict.REJECTED,
            issues=[VerificationIssue(VerificationSeverity.BLOCKER, "c", "d")],
        )
        assert blocked.has_blocker

    def test_to_dict(self):
        r = VerificationResult(
            verdict=VerificationVerdict.APPROVED_WITH_CHANGES,
            issues=[VerificationIssue(VerificationSeverity.MINOR, "style", "bad")],
            summary="ok",
            confidence=0.8,
        )
        d = r.to_dict()
        assert d["verdict"] == "APPROVED_WITH_CHANGES"
        assert d["issues"][0]["severity"] == "MINOR"
        assert d["confidence"] == 0.8


class TestGenerateVerifierPrompt:
    def test_contains_solution_and_context(self):
        p = generate_verifier_prompt(solution="def foo(): return 42", context="Bug fix")
        assert "def foo(): return 42" in p
        assert "Bug fix" in p

    def test_adversarial_and_readonly(self):
        p = generate_verifier_prompt("x = 1")
        assert "adversarial" in p.lower()
        assert "read-only" in p

    def test_type_hint(self):
        assert "logic errors" in generate_verifier_prompt("x", verification_type="code")

    def test_extra_checks(self):
        p = generate_verifier_prompt("x", extra_checks=["thread safety"])
        assert "thread safety" in p

    def test_json_format_instruction(self):
        assert "STRICT JSON" in generate_verifier_prompt("x")


class TestExtractJson:
    def test_plain(self):
        assert _extract_json('{"a": 1}') == {"a": 1}

    def test_fenced(self):
        assert _extract_json('```json\n{"a": 1}\n```') == {"a": 1}

    def test_with_prose(self):
        assert _extract_json('verdict:\n{"a": 1}\ndone') == {"a": 1}

    def test_invalid_returns_none(self):
        assert _extract_json("nope") is None


class TestClampConfidence:
    def test_bounds(self):
        assert _clamp_confidence(0.75) == 0.75
        assert _clamp_confidence(1.5) == 1.0
        assert _clamp_confidence(-0.5) == 0.0
        assert _clamp_confidence("high") == 0.0
        assert _clamp_confidence(None) == 0.0


class TestParseVerifierOutput:
    def test_approved(self):
        r = parse_verifier_output(
            json.dumps({
                "verdict": "APPROVED",
                "summary": "good",
                "confidence": 0.9,
                "issues": [],
            })
        )
        assert r.verdict == VerificationVerdict.APPROVED
        assert r.passed
        assert r.confidence == 0.9

    def test_rejected_with_issues(self):
        r = parse_verifier_output(
            json.dumps({
                "verdict": "REJECTED",
                "confidence": 0.95,
                "issues": [
                    {
                        "severity": "CRITICAL",
                        "category": "correctness",
                        "description": "bug",
                        "recommendation": "fix it",
                    }
                ],
            })
        )
        assert r.verdict == VerificationVerdict.REJECTED
        assert len(r.issues) == 1
        assert r.issues[0].severity == VerificationSeverity.CRITICAL
        assert r.issues[0].recommendation == "fix it"

    def test_fenced_output(self):
        r = parse_verifier_output('```json\n{"verdict": "APPROVED", "issues": []}\n```')
        assert r.verdict == VerificationVerdict.APPROVED

    def test_invalid_json_rejected(self):
        r = parse_verifier_output("not json")
        assert r.verdict == VerificationVerdict.REJECTED
        assert "not valid JSON" in r.summary

    def test_unknown_severity_defaults_major(self):
        r = parse_verifier_output(
            json.dumps({
                "verdict": "APPROVED",
                "issues": [{"severity": "WEIRD", "category": "x", "description": "y"}],
            })
        )
        assert r.issues[0].severity == VerificationSeverity.MAJOR

    def test_non_dict_issues_skipped(self):
        r = parse_verifier_output(
            json.dumps({
                "verdict": "APPROVED",
                "issues": [
                    "bad",
                    {"severity": "MINOR", "category": "x", "description": "y"},
                ],
            })
        )
        assert len(r.issues) == 1


class TestVerifyAdversarial:
    def test_no_llm_manual_dispatch(self):
        r = verify_adversarial("x = 1", context="test")
        assert r.metadata["dispatch_mode"] == "manual"
        assert "x = 1" in r.metadata["prompt"]

    def test_with_llm(self):
        mock_llm = MagicMock(
            return_value=json.dumps({
                "verdict": "APPROVED",
                "summary": "ok",
                "confidence": 0.9,
                "issues": [],
            })
        )
        r = verify_adversarial("def foo(): pass", llm_call=mock_llm)
        assert r.verdict == VerificationVerdict.APPROVED
        mock_llm.assert_called_once()

    def test_llm_failure(self):
        mock_llm = MagicMock(side_effect=RuntimeError("API down"))
        r = verify_adversarial("x", llm_call=mock_llm)
        assert r.verdict == VerificationVerdict.REJECTED
        assert "API down" in r.summary
