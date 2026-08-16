# -*- coding: utf-8 -*-
"""Tests for AUTOHARNESS harness synthesis prompt, schema, and generator (#2250, Slice A #2516)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evolution.lib.autoharness import (
    HARNESS_SPEC_JSON_SCHEMA,
    HARNESS_SYNTHESIS_SYSTEM_PROMPT,
    HarnessAssertion,
    HarnessSpec,
    TestCaseSpec,
    build_harness_synthesis_prompt,
    parse_skill_capabilities,
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
