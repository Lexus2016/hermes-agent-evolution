# -*- coding: utf-8 -*-
"""Tests for SkillHEX hypothesis→test self-verifier and tree search (Issue #2287)."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import pytest

from evolution.lib.skill_hex import (
    HypothesisVerifier,
    RevisionHypothesis,
    SkillHEXOptimizer,
    SkillRevisionNode,
    SkillRevisionTree,
    TestExecutionResult,
    VerificationTest,
    generate_hypotheses_from_failure,
)


def test_hypothesis_dataclass_and_serialization() -> None:
    hyp = RevisionHypothesis(
        hypothesis_id="hyp_1",
        description="Missing input parameter validation",
        failure_cause_category="precondition_missing",
        confidence=0.85,
        proposed_fix="Add check for required key",
        falsification_condition="Key is passed but None",
    )

    assert hyp.confidence == 0.85
    data = hyp.to_dict()
    assert data["hypothesis_id"] == "hyp_1"
    assert data["failure_cause_category"] == "precondition_missing"

    loaded = RevisionHypothesis.from_dict(data)
    assert loaded.hypothesis_id == hyp.hypothesis_id
    assert loaded.confidence == hyp.confidence


def test_verification_test_and_assertion_evaluation() -> None:
    test_exit = VerificationTest(
        test_id="t1",
        hypothesis_id="hyp_1",
        name="Exit code check",
        test_input={"action": "run"},
        expected_behavior="Return 0",
        assertion_type="exit_code",
        expected_value=0,
    )
    passed, ev = HypothesisVerifier.evaluate_test_assertion(
        test_exit, {"return_value": 0}
    )
    assert passed is True
    passed_fail, _ = HypothesisVerifier.evaluate_test_assertion(
        test_exit, {"return_value": 1}
    )
    assert passed_fail is False

    test_contains = VerificationTest(
        test_id="t2",
        hypothesis_id="hyp_1",
        name="Output contains check",
        test_input={"action": "run"},
        expected_behavior="Contains success",
        assertion_type="output_contains",
        expected_value="SUCCESS",
    )
    passed, ev = HypothesisVerifier.evaluate_test_assertion(
        test_contains, {"stdout": "All operations completed: SUCCESS"}
    )
    assert passed is True

    test_json = VerificationTest(
        test_id="t3",
        hypothesis_id="hyp_1",
        name="JSON valid check",
        test_input={},
        expected_behavior="Valid JSON",
        assertion_type="json_valid",
    )
    passed, _ = HypothesisVerifier.evaluate_test_assertion(
        test_json, {"stdout": '{"result": 123}'}
    )
    assert passed is True
    passed_bad, _ = HypothesisVerifier.evaluate_test_assertion(
        test_json, {"stdout": "not json"}
    )
    assert passed_bad is False

    test_custom = VerificationTest(
        test_id="t4",
        hypothesis_id="hyp_1",
        name="Custom eval check",
        test_input={},
        expected_behavior="Custom logic",
        assertion_type="custom_eval",
        expected_value=lambda out: out.get("custom_metric", 0) > 10,
    )
    passed, _ = HypothesisVerifier.evaluate_test_assertion(
        test_custom, {"custom_metric": 15}
    )
    assert passed is True


def test_hypothesis_verifier_dense_evidence() -> None:
    hyp = RevisionHypothesis(
        hypothesis_id="hyp_edge",
        description="Fails on empty input array",
        failure_cause_category="boundary_case",
        confidence=0.9,
        proposed_fix="Handle empty array gracefully",
    )

    tests = HypothesisVerifier.generate_verification_tests(
        hyp, skill_name="array_sorter"
    )
    assert len(tests) == 2

    # Runner that passes both tests
    def passing_runner(inp: dict) -> dict:
        return {"stdout": "success Handle empty array gracefully", "return_value": 0}

    score, results = HypothesisVerifier.verify_hypothesis(hyp, passing_runner, tests)
    assert score == 1.0
    assert len(results) == 2
    assert all(r.passed for r in results)

    # Runner that fails
    def failing_runner(inp: dict) -> dict:
        return {"stdout": "fatal error", "return_value": 1}

    score_fail, results_fail = HypothesisVerifier.verify_hypothesis(
        hyp, failing_runner, tests
    )
    assert score_fail == 0.0
    assert not any(r.passed for r in results_fail)


def test_skill_revision_node_ucb_calculation() -> None:
    node = SkillRevisionNode(
        node_id="node_1",
        parent_id="node_0",
        skill_name="my_skill",
        skill_code="def run(): pass",
        visit_count=4,
        value=3.2,  # mean = 0.8
        evidence_score=0.9,
    )

    # Exploitation = 0.8, exploration = 1.414 * sqrt(ln(16) / 4) = 1.414 * sqrt(2.7725 / 4) ~= 1.177
    # Evidence term = 0.5 * 0.9 = 0.45
    # Total UCB ~= 0.8 + 1.177 + 0.45 = 2.427
    ucb = node.ucb_score(parent_visits=16)
    assert 2.3 < ucb < 2.6


def test_skill_revision_tree_expansion_and_backprop() -> None:
    root = SkillRevisionNode(
        node_id="root",
        parent_id=None,
        skill_name="text_cleaner",
        skill_code="def clean(t): return t",
        visit_count=1,
        value=0.4,
    )
    tree = SkillRevisionTree(root)

    hyp1 = RevisionHypothesis(
        hypothesis_id="h1", description="Strip whitespace", proposed_fix="t.strip()"
    )
    hyp2 = RevisionHypothesis(
        hypothesis_id="h2", description="Remove control chars", proposed_fix="re.sub"
    )

    def mock_generator(code: str, hyp: RevisionHypothesis) -> str:
        return f"{code} # fix: {hyp.proposed_fix}"

    children = tree.expand(
        node_id="root",
        hypotheses=[hyp1, hyp2],
        revision_generator=mock_generator,
    )

    assert len(children) == 2
    assert "root_rev_1" in tree.nodes
    assert "root_rev_2" in tree.nodes
    assert len(root.children_ids) == 2

    # Test selection
    selected = tree.select_node()
    assert selected.node_id in ("root_rev_1", "root_rev_2")

    # Backpropagation
    tree.backpropagate(node_id="root_rev_1", reward=0.95, evidence_score=0.9)
    assert tree.nodes["root_rev_1"].visit_count == 1
    assert tree.nodes["root_rev_1"].value == 0.95
    assert tree.root.visit_count == 2

    # Best node
    best = tree.best_node()
    assert best.node_id == "root_rev_1"
    assert best.mean_value() == 0.95

    # Serialization roundtrip
    data = tree.to_dict()
    loaded_tree = SkillRevisionTree.from_dict(data)
    assert loaded_tree.root_id == "root"
    assert len(loaded_tree.nodes) == 3


def test_generate_hypotheses_from_failure() -> None:
    h_syntax = generate_hypotheses_from_failure(
        "code_gen", {"stderr": "SyntaxError: invalid syntax"}
    )
    assert any(h.hypothesis_id == "hyp_syntax_ast" for h in h_syntax)

    h_key = generate_hypotheses_from_failure(
        "api_tool", {"stderr": "KeyError: 'auth_token'"}
    )
    assert any(h.hypothesis_id == "hyp_missing_key" for h in h_key)

    h_timeout = generate_hypotheses_from_failure(
        "scraper", {"stderr": "Operation timed out after 30s"}
    )
    assert any(h.hypothesis_id == "hyp_timeout" for h in h_timeout)


def test_skill_hex_optimizer_end_to_end() -> None:
    initial_code = "def process(x): return x * 2"
    target_skill_name = "math_processor"

    def hypothesis_gen(skill_name: str, info: dict) -> List[RevisionHypothesis]:
        return [
            RevisionHypothesis(
                hypothesis_id="h_type_check",
                description="Needs type conversion for strings",
                proposed_fix="x = int(x)",
                confidence=0.8,
            ),
            RevisionHypothesis(
                hypothesis_id="h_dead_end",
                description="Wrong hypothesis",
                proposed_fix="# wrong fix",
                confidence=0.3,
            ),
        ]

    def revision_gen(code: str, hyp: RevisionHypothesis) -> str:
        return f"{code}\n    {hyp.proposed_fix}"

    def evaluator(code: str) -> float:
        if "int(x)" in code:
            return 0.95
        if "# wrong fix" in code:
            return 0.2
        return 0.5

    optimizer = SkillHEXOptimizer(exploration_weight=1.414, evidence_weight=0.5)
    res = optimizer.run_tree_search(
        skill_name=target_skill_name,
        initial_skill_code=initial_code,
        hypothesis_generator=hypothesis_gen,
        revision_generator=revision_gen,
        evaluator=evaluator,
        max_iterations=3,
        target_score_threshold=0.9,
    )

    assert res["converged"] is True
    assert res["best_score"] == 0.95
    assert "int(x)" in res["best_code"]
    assert res["total_iterations"] >= 1
