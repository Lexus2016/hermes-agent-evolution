"""Tests for agent.adversarial_verification — verifier prompt generation and output parsing."""

import json
import pytest
from agent.adversarial_verification import (
    VerificationIssue,
    VerificationResult,
    VerificationSeverity,
    VerificationVerdict,
    detect_model_family,
    format_verification_result,
    generate_verifier_prompt,
    get_verifier_system_prompt,
    parse_verifier_output,
    resolve_verifier_model,
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


# ── Model-family diversity tests (#909) ─────────────────────────────────


class TestDetectModelFamily:
    def test_claude_models(self):
        assert detect_model_family("anthropic/claude-opus-4.8") == "anthropic"
        assert detect_model_family("claude-sonnet-4-6") == "anthropic"

    def test_openai_models(self):
        assert detect_model_family("gpt-5.5") == "openai"
        assert detect_model_family("openai/gpt-4o") == "openai"

    def test_google_models(self):
        assert detect_model_family("google/gemini-3-flash-preview") == "google"

    def test_xai_models(self):
        assert detect_model_family("x-ai/grok-4") == "xai"

    def test_meta_models(self):
        assert detect_model_family("meta-llama/llama-4") == "meta"

    def test_deepseek_models(self):
        assert detect_model_family("deepseek/deepseek-v4-pro") == "deepseek"

    def test_qwen_models(self):
        assert detect_model_family("qwen/qwen3-max") == "qwen"

    def test_mistral_models(self):
        assert detect_model_family("mistralai/mixtral-8x22b") == "mistral"

    def test_nous_hermes_models(self):
        assert detect_model_family("nousresearch/hermes-4-70b") == "nous"

    def test_luminous_not_misfiled_as_nous(self):
        # "nous" is a substring of "luminous" (Aleph Alpha) — ordering in the
        # keyword table must classify it as its own family, not Nous Research.
        assert detect_model_family("luminous-supreme") == "alephalpha"

    def test_unknown_model(self):
        assert detect_model_family("some-obscure-local-model") == "unknown"

    def test_empty_or_none(self):
        assert detect_model_family("") == "unknown"
        assert detect_model_family(None) == "unknown"

    def test_case_insensitive(self):
        assert detect_model_family("CLAUDE-Opus-4.8") == "anthropic"


class TestResolveVerifierModel:
    def test_explicit_cross_family_model(self):
        result = resolve_verifier_model(
            "anthropic/claude-opus-4.8",
            {"model": "google/gemini-3-flash-preview", "provider": "openrouter"},
        )
        assert result["model"] == "google/gemini-3-flash-preview"
        assert result["provider"] == "openrouter"
        assert result["generator_family"] == "anthropic"
        assert result["verifier_family"] == "google"
        assert result["cross_family"] is True

    def test_explicit_same_family_model(self):
        result = resolve_verifier_model(
            "anthropic/claude-opus-4.8",
            {"model": "claude-sonnet-4-6", "provider": ""},
        )
        assert result["cross_family"] is False
        assert result["generator_family"] == "anthropic"
        assert result["verifier_family"] == "anthropic"

    def test_explicit_model_unknown_family(self):
        result = resolve_verifier_model(
            "anthropic/claude-opus-4.8",
            {"model": "some-local-model"},
        )
        assert result["cross_family"] is None
        assert result["verifier_family"] == "unknown"

    def test_provider_only_override_is_unknown(self):
        result = resolve_verifier_model(
            "anthropic/claude-opus-4.8",
            {"provider": "openrouter", "model": ""},
        )
        assert result["cross_family"] is None
        assert result["model"] == ""
        assert result["provider"] == "openrouter"

    def test_no_config_is_same_family(self):
        result = resolve_verifier_model("anthropic/claude-opus-4.8", None)
        assert result["cross_family"] is False
        assert result["generator_family"] == "anthropic"
        assert result["verifier_family"] == "anthropic"

    def test_auto_provider_no_config_is_same_family(self):
        result = resolve_verifier_model("gpt-5.5", {"provider": "auto", "model": ""})
        assert result["cross_family"] is False
        assert result["generator_family"] == "openai"

    def test_no_config_unrecognized_generator_still_same_family(self):
        # Auto/empty config inherits the generator's exact model, so it is
        # same-family even when we don't recognize the family (#909 review).
        result = resolve_verifier_model("some-local-model", {})
        assert result["cross_family"] is False
        assert result["generator_family"] == "unknown"

    def test_empty_generator_model(self):
        result = resolve_verifier_model(None, {})
        assert result["generator_family"] == "unknown"
        assert result["cross_family"] is None

    def test_identical_unrecognized_models_are_same_family(self):
        # Same exact unrecognized model string on both sides — same blind
        # spots — must be flagged same-family even though the family is
        # "unknown" (#909 review, false-negative fix).
        result = resolve_verifier_model(
            "some-local-model", {"model": "some-local-model"}
        )
        assert result["cross_family"] is False
        assert result["generator_family"] == "unknown"
        assert result["verifier_family"] == "unknown"

    def test_luminous_vs_nous_is_cross_family(self):
        # Regression guard for the substring collision: a luminous generator
        # and a nous verifier are different families, not the same.
        result = resolve_verifier_model("luminous-supreme", {"model": "nous-hermes-2"})
        assert result["generator_family"] == "alephalpha"
        assert result["verifier_family"] == "nous"
        assert result["cross_family"] is True

    def test_single_family_provider_override_detects_family(self):
        # Provider-only override on a single-family provider identifies the
        # family from the provider name (#909 review, false-negative fix).
        cross = resolve_verifier_model(
            "openai/gpt-4o", {"provider": "anthropic", "model": ""}
        )
        assert cross["verifier_family"] == "anthropic"
        assert cross["cross_family"] is True

        same = resolve_verifier_model(
            "anthropic/claude-opus-4.8", {"provider": "anthropic", "model": ""}
        )
        assert same["verifier_family"] == "anthropic"
        assert same["cross_family"] is False

    def test_multi_model_provider_override_stays_unknown(self):
        # Aggregators like openrouter can't be pinned to one family — degrade
        # to None rather than guessing.
        result = resolve_verifier_model(
            "anthropic/claude-opus-4.8", {"provider": "openrouter", "model": ""}
        )
        assert result["verifier_family"] == "unknown"
        assert result["cross_family"] is None
