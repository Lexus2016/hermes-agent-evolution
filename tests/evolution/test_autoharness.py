# -*- coding: utf-8 -*-
"""Tests for AUTOHARNESS harness synthesis prompt, schema, and generator (#2250, Slice A #2516)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evolution.lib.autoharness import (
    HARNESS_SPEC_JSON_SCHEMA,
    HARNESS_SYNTHESIS_SYSTEM_PROMPT,
    AssertionResult,
    HarnessAssertion,
    HarnessDiagnosticReport,
    HarnessSpec,
    TestCaseResult,
    TestCaseSpec,
    build_harness_synthesis_prompt,
    evaluate_assertion,
    parse_skill_capabilities,
    run_harness,
    synthesize_harness_spec,
)


def test_harness_spec_serialization_roundtrip() -> None:
    assertion = HarnessAssertion(
        assertion_type="exit_code",
        target="return_value",
        expected=0,
        weight=1.0,
        description="Check return code",
    )
    tc = TestCaseSpec(
        test_id="tc_1",
        name="Nominal execution",
        input_data={"param": "value"},
        expected_behavior="Returns 0",
        assertions=[assertion],
        timeout_sec=25.0,
    )
    spec = HarnessSpec(
        skill_name="test_skill",
        skill_category="coding",
        claimed_capabilities=["refactor code", "parse AST"],
        test_cases=[tc],
        passing_threshold=0.85,
        metadata={"author": "autoharness"},
    )

    d = spec.to_dict()
    assert d["skill_name"] == "test_skill"
    assert d["passing_threshold"] == 0.85
    assert len(d["test_cases"]) == 1
    assert d["test_cases"][0]["assertions"][0]["assertion_type"] == "exit_code"

    # From dict
    spec_from_dict = HarnessSpec.from_dict(d)
    assert spec_from_dict.skill_name == spec.skill_name
    assert len(spec_from_dict.test_cases) == 1
    assert spec_from_dict.test_cases[0].assertions[0].expected == 0

    # JSON roundtrip
    json_str = spec.to_json()
    assert "test_skill" in json_str
    spec_from_json = HarnessSpec.from_json(json_str)
    assert spec_from_json.skill_category == "coding"
    assert spec_from_json.claimed_capabilities == ["refactor code", "parse AST"]


def test_parse_skill_capabilities_from_dict() -> None:
    skill_dict = {
        "name": "data_cleaner",
        "category": "data_processing",
        "description": "Cleans CSV files and normalizes schemas",
        "capabilities": ["remove null rows", "standardize column headers"],
    }
    parsed = parse_skill_capabilities(skill_dict)
    assert parsed["name"] == "data_cleaner"
    assert parsed["category"] == "data_processing"
    assert len(parsed["capabilities"]) == 2
    assert "remove null rows" in parsed["capabilities"]


def test_parse_skill_capabilities_from_markdown() -> None:
    md = """---
name: code_refactor_pro
category: coding
description: Advanced Python code refactoring tool
---

# Code Refactor Pro

## Capabilities
- AST-level syntax transformation
- Safe variable renaming
- Unused import elimination
"""
    parsed = parse_skill_capabilities(md)
    assert parsed["name"] == "code_refactor_pro"
    assert parsed["category"] == "coding"
    assert len(parsed["capabilities"]) == 3
    assert "AST-level syntax transformation" in parsed["capabilities"]
    assert "Unused import elimination" in parsed["capabilities"]


def test_build_harness_synthesis_prompt() -> None:
    skill_dict = {
        "name": "search_accelerator",
        "category": "search_retrieval",
        "description": "Fast hybrid vector-BM25 retrieval",
        "capabilities": ["Dense vector query", "BM25 keyword search"],
    }
    prompt = build_harness_synthesis_prompt(skill_dict)
    assert "search_accelerator" in prompt
    assert "search_retrieval" in prompt
    assert "Dense vector query" in prompt
    assert "BM25 keyword search" in prompt
    assert "HarnessSpec" in prompt


def test_synthesize_harness_spec_coding_category() -> None:
    skill_dict = {
        "name": "ast_patcher",
        "category": "coding",
        "capabilities": ["Apply unified diff", "Verify AST syntax"],
    }
    spec = synthesize_harness_spec(skill_dict)
    assert isinstance(spec, HarnessSpec)
    assert spec.skill_name == "ast_patcher"
    assert spec.skill_category == "coding"
    assert len(spec.test_cases) == 3  # 2 capabilities nominal + 1 error boundary
    assert any("nominal" in tc.test_id for tc in spec.test_cases)
    assert any("error_boundary" in tc.test_id for tc in spec.test_cases)
    assert spec.passing_threshold >= 0.8


def test_synthesize_harness_spec_data_processing() -> None:
    skill_dict = {
        "name": "json_transformer",
        "category": "data_processing",
        "capabilities": ["Normalize JSON schema", "Filter invalid records"],
    }
    spec = synthesize_harness_spec(skill_dict)
    assert spec.skill_category == "data_processing"
    assert len(spec.test_cases) == 2
    for tc in spec.test_cases:
        assert any(a.assertion_type == "json_valid" for a in tc.assertions)


def test_synthesize_harness_spec_search_retrieval() -> None:
    skill_dict = {
        "name": "doc_retriever",
        "category": "search_retrieval",
        "capabilities": ["Retrieve top-k documents"],
    }
    spec = synthesize_harness_spec(skill_dict)
    assert spec.skill_category == "search_retrieval"
    assert len(spec.test_cases) == 1
    assert any(
        a.assertion_type == "output_contains" for a in spec.test_cases[0].assertions
    )


def test_synthesize_harness_spec_workflow_automation() -> None:
    skill_dict = {
        "name": "pr_sweeper",
        "category": "workflow_automation",
        "capabilities": ["Triage open PRs", "Close stale branches"],
    }
    spec = synthesize_harness_spec(skill_dict)
    assert spec.skill_category == "workflow_automation"
    assert len(spec.test_cases) == 2
    for tc in spec.test_cases:
        assert any(a.assertion_type == "exit_code" for a in tc.assertions)


def test_schema_and_system_prompt_constants() -> None:
    assert isinstance(HARNESS_SPEC_JSON_SCHEMA, dict)
    assert "properties" in HARNESS_SPEC_JSON_SCHEMA
    assert "AUTOHARNESS" in HARNESS_SYNTHESIS_SYSTEM_PROMPT


def test_evaluate_assertion_types() -> None:
    # 1. exit_code
    a_exit = HarnessAssertion(
        assertion_type="exit_code", target="return_value", expected=0
    )
    assert evaluate_assertion(a_exit, {"return_value": 0}).passed is True
    assert evaluate_assertion(a_exit, {"return_value": 1}).passed is False

    # 2. output_contains
    a_contains = HarnessAssertion(
        assertion_type="output_contains", target="stdout", expected="success"
    )
    assert (
        evaluate_assertion(
            a_contains, {"stdout": "Operation completed with success!"}
        ).passed
        is True
    )
    assert (
        evaluate_assertion(a_contains, {"stdout": "Operation failed"}).passed is False
    )

    # 3. regex_match
    a_regex = HarnessAssertion(
        assertion_type="regex_match", target="stdout", expected=r"v\d+\.\d+"
    )
    assert (
        evaluate_assertion(a_regex, {"stdout": "version v2.4 released"}).passed is True
    )
    assert evaluate_assertion(a_regex, {"stdout": "version unknown"}).passed is False

    # 4. json_valid
    a_json = HarnessAssertion(
        assertion_type="json_valid", target="stdout", expected=True
    )
    assert evaluate_assertion(a_json, {"stdout": '{"status": "ok"}'}).passed is True
    assert evaluate_assertion(a_json, {"stdout": "not a json"}).passed is False

    # 5. execution_time_under
    a_time = HarnessAssertion(
        assertion_type="execution_time_under", target="state", expected=2.0
    )
    assert evaluate_assertion(a_time, {"execution_time_sec": 1.2}).passed is True
    assert evaluate_assertion(a_time, {"execution_time_sec": 3.5}).passed is False

    # 6. custom_eval
    a_custom = HarnessAssertion(
        assertion_type="custom_eval", target="artifacts", expected=lambda x: len(x) > 0
    )
    assert evaluate_assertion(a_custom, {"artifacts": ["f1.py"]}).passed is True
    assert evaluate_assertion(a_custom, {"artifacts": []}).passed is False


def test_run_harness_successful_execution() -> None:
    skill_dict = {
        "name": "math_solver",
        "category": "coding",
        "capabilities": ["Add two numbers", "Multiply numbers"],
    }
    spec = synthesize_harness_spec(skill_dict)

    def dummy_runner(input_data: dict) -> dict:
        return {
            "stdout": json.dumps({"status": "ok", "result": 42}),
            "stderr": "",
            "return_value": 0,
            "state": {},
        }

    report = run_harness(spec, dummy_runner)
    assert isinstance(report, HarnessDiagnosticReport)
    assert report.skill_name == "math_solver"
    assert report.total_cases == len(spec.test_cases)
    # The nominal cases should pass
    assert report.passed_cases >= 2
    assert report.overall_score > 0.6
    assert "per_criterion_diagnostics" in report.to_dict()
    assert (
        report.to_dict()["per_criterion_diagnostics"]["exit_code"]["total_assertions"]
        >= 2
    )


def test_run_harness_diagnostics_and_delta() -> None:
    skill_dict = {
        "name": "data_pipeline",
        "category": "data_processing",
        "capabilities": ["Normalize payload", "Filter rows"],
    }
    spec = synthesize_harness_spec(skill_dict)

    # Prior runner fails one case
    def prior_runner(input_data: dict) -> dict:
        op = input_data.get("operation", "")
        if "Filter" in op:
            return {"stdout": "invalid non-json", "stderr": "error", "return_value": 1}
        return {
            "stdout": '{"clean": true}',
            "stderr": "",
            "return_value": 0,
            "execution_time_sec": 1.0,
        }

    # Improved runner passes both
    def improved_runner(input_data: dict) -> dict:
        return {
            "stdout": '{"clean": true, "filtered": 5}',
            "stderr": "",
            "return_value": 0,
            "execution_time_sec": 0.5,
        }

    prior_report = run_harness(spec, prior_runner)
    improved_report = run_harness(spec, improved_runner, prior_report=prior_report)

    assert prior_report.overall_score < improved_report.overall_score
    assert improved_report.score_delta is not None
    assert improved_report.score_delta > 0.0
    assert improved_report.prior_score == prior_report.overall_score
    assert improved_report.passed is True

    # Diagnostic serialization
    report_json = improved_report.to_json()
    assert "score_delta" in report_json
    deserialized = HarnessDiagnosticReport.from_json(report_json)
    assert deserialized.score_delta == improved_report.score_delta
    assert deserialized.passed is True


def test_run_harness_exception_resilience() -> None:
    skill_dict = {
        "name": "crashing_skill",
        "category": "general",
        "capabilities": ["Safe operation"],
    }
    spec = synthesize_harness_spec(skill_dict)

    def crashing_runner(input_data: dict) -> dict:
        raise RuntimeError("Uncaught fatal crash in skill execution")

    report = run_harness(spec, crashing_runner)
    assert report.passed is False
    assert report.failed_cases == 1
    assert report.test_case_results[0].error is not None
    assert "Uncaught fatal crash" in str(report.test_case_results[0].error)
