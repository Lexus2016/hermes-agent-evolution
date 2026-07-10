"""Tests for agent.adversarial_verification — verifier prompt generation and output parsing."""

import json
import pytest
from agent.adversarial_verification import (
    VerificationIssue,
    VerificationResult,
    VerificationSeverity,
    VerificationVerdict,
    format_verification_result,
    generate_verifier_prompt,
    get_verifier_system_prompt,
    parse_verifier_output,
)


# ── Prompt generation tests ─────────────────────────────────────────────


class TestGenerateVerifierPrompt:
    def test_includes_solution(self):
        prompt = generate_verifier_prompt("def foo(): pass", solution_type="code")
        assert "def foo(): pass" in prompt

    def test_includes_context(self):
        prompt = generate_verifier_prompt("solution", context="original task", solution_type="code")
        assert "original task" in prompt

    def test_includes_solution_type(self):
        prompt = generate_verifier_prompt("x", solution_type="research_report")
        assert "research_report" in prompt

    def test_empty_context_ok(self):
        prompt = generate_verifier_prompt("x", solution_type="code")
        assert "## Solution to Verify" in prompt

    def test_system_prompt_has_adversarial_language(self):
        system = get_verifier_system_prompt()
        assert "FIND PROBLEMS" in system
        assert "adversarial" in system.lower()
        assert "verdict" in system.lower()


# ── Output parsing tests ────────────────────────────────────────────────


class TestParseVerifierOutput:
    def test_parses_approved_no_issues(self):
        output = """```json
{
  "verdict": "approved",
  "confidence": 0.9,
  "summary": "No issues found",
  "issues": []
}
```"""
        result = parse_verifier_output(output)
        assert result.verdict == VerificationVerdict.APPROVED
        assert result.confidence == 0.9
        assert len(result.issues) == 0
        assert result.summary == "No issues found"

    def test_parses_rejected_with_issues(self):
        output = """```json
{
  "verdict": "rejected",
  "confidence": 0.85,
  "summary": "Critical bug found",
  "issues": [
    {
      "severity": "critical",
      "category": "correctness",
      "description": "Null pointer dereference",
      "location": "foo.py:42",
      "evidence": "bar = foo.baz",
      "recommendation": "Add null check"
    }
  ]
}
```"""
        result = parse_verifier_output(output)
        assert result.verdict == VerificationVerdict.REJECTED
        assert result.confidence == 0.85
        assert len(result.issues) == 1
        issue = result.issues[0]
        assert issue.severity == VerificationSeverity.CRITICAL
        assert issue.category == "correctness"
        assert "Null pointer" in issue.description
        assert issue.location == "foo.py:42"
        assert issue.recommendation == "Add null check"

    def test_parses_approved_with_changes(self):
        output = """```json
{
  "verdict": "approved_with_changes",
  "confidence": 0.7,
  "summary": "Minor style issues",
  "issues": [
    {"severity": "minor", "category": "style", "description": "Bad naming", "recommendation": "Rename"}
  ]
}
```"""
        result = parse_verifier_output(output)
        assert result.verdict == VerificationVerdict.APPROVED_WITH_CHANGES
        assert len(result.issues) == 1
        assert result.issues[0].severity == VerificationSeverity.MINOR

    def test_handles_multiple_issues(self):
        output = """```json
{
  "verdict": "rejected",
  "confidence": 0.95,
  "summary": "Multiple issues",
  "issues": [
    {"severity": "major", "category": "security", "description": "SQL injection"},
    {"severity": "minor", "category": "style", "description": "Long line"},
    {"severity": "info", "category": "performance", "description": "O(n^2)"}
  ]
}
```"""
        result = parse_verifier_output(output)
        assert len(result.issues) == 3
        assert result.issues[0].severity == VerificationSeverity.MAJOR
        assert result.issues[1].severity == VerificationSeverity.MINOR
        assert result.issues[2].severity == VerificationSeverity.INFO

    def test_unparseable_output_defaults_to_approved(self):
        result = parse_verifier_output("This is not JSON at all")
        assert result.verdict == VerificationVerdict.APPROVED
        assert result.confidence == 0.0
        assert "Could not parse" in result.summary

    def test_empty_output_defaults_to_approved(self):
        result = parse_verifier_output("")
        assert result.verdict == VerificationVerdict.APPROVED

    def test_malformed_json_defaults_to_approved(self):
        output = """```json
{invalid json
```"""
        result = parse_verifier_output(output)
        assert result.verdict == VerificationVerdict.APPROVED

    def test_raw_output_preserved(self):
        output = "some raw text"
        result = parse_verifier_output(output)
        assert result.raw_output == "some raw text"

    def test_bare_json_without_codeblock(self):
        output = '{"verdict": "approved", "confidence": 0.5, "summary": "ok", "issues": []}'
        result = parse_verifier_output(output)
        assert result.verdict == VerificationVerdict.APPROVED
        assert result.confidence == 0.5

    def test_unknown_severity_defaults_to_info(self):
        output = """```json
{"verdict": "approved", "issues": [{"severity": "unknown_sev", "category": "x", "description": "d"}]}
```"""
        result = parse_verifier_output(output)
        assert len(result.issues) == 1
        assert result.issues[0].severity == VerificationSeverity.INFO

    def test_unknown_verdict_defaults_to_approved(self):
        output = """```json
{"verdict": "maybe", "issues": []}
```"""
        result = parse_verifier_output(output)
        assert result.verdict == VerificationVerdict.APPROVED


# ── Formatting tests ────────────────────────────────────────────────────


class TestFormatVerificationResult:
    def test_approved_no_issues(self):
        result = VerificationResult(
            verdict=VerificationVerdict.APPROVED,
            summary="All good",
            confidence=0.9,
        )
        formatted = format_verification_result(result)
        assert "APPROVED" in formatted
        assert "All good" in formatted
        assert "No issues found" in formatted

    def test_rejected_with_issues(self):
        result = VerificationResult(
            verdict=VerificationVerdict.REJECTED,
            summary="Bad",
            issues=[
                VerificationIssue(
                    severity=VerificationSeverity.CRITICAL,
                    category="correctness",
                    description="Bug found",
                    location="line 42",
                    recommendation="Fix it",
                ),
            ],
        )
        formatted = format_verification_result(result)
        assert "REJECTED" in formatted
        assert "Bug found" in formatted
        assert "Fix it" in formatted
        assert "CRITICAL" in formatted

    def test_confidence_percentage(self):
        result = VerificationResult(
            verdict=VerificationVerdict.APPROVED,
            confidence=0.85,
        )
        formatted = format_verification_result(result)
        assert "85%" in formatted


# ── Enum completeness ───────────────────────────────────────────────────


class TestEnums:
    def test_all_severities_have_values(self):
        for sev in VerificationSeverity:
            assert isinstance(sev.value, str)

    def test_all_verdicts_have_values(self):
        for verdict in VerificationVerdict:
            assert isinstance(verdict.value, str)