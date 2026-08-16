# -*- coding: utf-8 -*-
"""Hypothesis→test self-verifier + tree search for skill revision (SkillHEX, issue #2287).

Adopts SkillHEX (arXiv:2608.05628 cs.AI Aug 2026):
1. Hypothesis-driven self-verification: transforms explicit failure-cause hypotheses into
   executable verification tests that produce dense diagnostic evidence without greedy lock-in.
2. Evidence-guided tree search over skill revision branches: dynamically balances exploitation
   of evidence-supported revisions against exploration of alternative failure diagnoses (UCB1
   augmented with dense verification scores).
3. Reversible revision branches to avoid single-incumbent local optima traps.
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

logger = logging.getLogger(__name__)

__all__ = [
    "HypothesisVerifier",
    "RevisionHypothesis",
    "SkillHEXOptimizer",
    "SkillRevisionNode",
    "SkillRevisionTree",
    "TestExecutionResult",
    "VerificationTest",
    "generate_hypotheses_from_failure",
]


@dataclass
class RevisionHypothesis:
    """Explicit, falsifiable hypothesis explaining why a skill failed and how to revise it."""

    hypothesis_id: str
    description: str
    failure_cause_category: str = "logic_error"
    confidence: float = 0.5
    proposed_fix: str = ""
    falsification_condition: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.confidence = max(0.0, min(1.0, float(self.confidence)))

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> RevisionHypothesis:
        return cls(
            hypothesis_id=str(data.get("hypothesis_id", "")),
            description=str(data.get("description", "")),
            failure_cause_category=str(
                data.get("failure_cause_category", "logic_error")
            ),
            confidence=float(data.get("confidence", 0.5)),
            proposed_fix=str(data.get("proposed_fix", "")),
            falsification_condition=str(data.get("falsification_condition", "")),
            metadata=dict(data.get("metadata", {})),
        )


@dataclass
class VerificationTest:
    """Executable verification test specifically synthesized to validate or falsify a hypothesis."""

    __test__ = False  # Prevent pytest from collecting this as a test class

    test_id: str
    hypothesis_id: str
    name: str
    test_input: Dict[str, Any]
    expected_behavior: str
    assertion_type: str = "output_contains"  # output_contains, exit_code, regex_match, json_valid, custom_eval
    expected_value: Any = None
    weight: float = 1.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "test_id": self.test_id,
            "hypothesis_id": self.hypothesis_id,
            "name": self.name,
            "test_input": self.test_input,
            "expected_behavior": self.expected_behavior,
            "assertion_type": self.assertion_type,
            "expected_value": self.expected_value,
            "weight": self.weight,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> VerificationTest:
        return cls(
            test_id=str(data.get("test_id", "")),
            hypothesis_id=str(data.get("hypothesis_id", "")),
            name=str(data.get("name", "")),
            test_input=dict(data.get("test_input", {})),
            expected_behavior=str(data.get("expected_behavior", "")),
            assertion_type=str(data.get("assertion_type", "output_contains")),
            expected_value=data.get("expected_value"),
            weight=float(data.get("weight", 1.0)),
        )


@dataclass
class TestExecutionResult:
    """Execution result and diagnostic evidence of running a verification test."""

    __test__ = False  # Prevent pytest from collecting this as a test class

    test_id: str
    hypothesis_id: str
    passed: bool
    actual_output: Dict[str, Any]
    evidence: str = ""
    score: float = 0.0
    execution_time_sec: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> TestExecutionResult:
        return cls(
            test_id=str(data.get("test_id", "")),
            hypothesis_id=str(data.get("hypothesis_id", "")),
            passed=bool(data.get("passed", False)),
            actual_output=dict(data.get("actual_output", {})),
            evidence=str(data.get("evidence", "")),
            score=float(data.get("score", 0.0)),
            execution_time_sec=float(data.get("execution_time_sec", 0.0)),
        )


class HypothesisVerifier:
    """Synthesizes verification tests from failure hypotheses and computes dense diagnostic evidence."""

    @staticmethod
    def generate_verification_tests(
        hypothesis: RevisionHypothesis,
        skill_name: str = "target_skill",
    ) -> List[VerificationTest]:
        """Synthesize concrete executable verification tests from a failure-cause hypothesis."""
        tests: List[VerificationTest] = []
        hid = hypothesis.hypothesis_id

        # 1. Direct validation test for the proposed fix
        tests.append(
            VerificationTest(
                test_id=f"{hid}_test_fix_validation",
                hypothesis_id=hid,
                name=f"Verify fix for {hypothesis.failure_cause_category}",
                test_input={
                    "scenario": "fix_validation",
                    "target": skill_name,
                    "fix": hypothesis.proposed_fix,
                },
                expected_behavior=f"Skill executes without {hypothesis.failure_cause_category} and succeeds.",
                assertion_type="exit_code",
                expected_value=0,
                weight=1.0,
            )
        )

        # 2. Boundary / Falsification condition test
        tests.append(
            VerificationTest(
                test_id=f"{hid}_test_falsification_probe",
                hypothesis_id=hid,
                name="Probe falsification boundary condition",
                test_input={
                    "scenario": "falsification_probe",
                    "condition": hypothesis.falsification_condition,
                },
                expected_behavior="Does not reproduce the original failure symptom under boundary conditions.",
                assertion_type="output_contains",
                expected_value=hypothesis.proposed_fix or "success",
                weight=0.8,
            )
        )

        return tests

    @staticmethod
    def evaluate_test_assertion(
        test: VerificationTest,
        execution_output: Dict[str, Any],
    ) -> Tuple[bool, str]:
        """Evaluate a test assertion against execution outputs."""
        atype = test.assertion_type
        exp = test.expected_value
        stdout = str(execution_output.get("stdout", ""))
        stderr = str(execution_output.get("stderr", ""))
        rc = execution_output.get("return_value", 0)

        if atype == "exit_code":
            try:
                passed = int(rc) == int(exp)
                return passed, f"return_value={rc} (expected {exp})"
            except (ValueError, TypeError):
                return False, f"invalid return_value={rc}"

        elif atype == "output_contains":
            target_str = stdout if stdout else stderr
            exp_str = str(exp or "")
            passed = (
                exp_str.lower() in target_str.lower() if exp_str else bool(target_str)
            )
            return passed, f"output contains '{exp_str}': {passed}"

        elif atype == "regex_match":
            target_str = stdout or stderr
            passed = bool(re.search(str(exp), target_str))
            return passed, f"regex '{exp}' matched: {passed}"

        elif atype == "json_valid":
            try:
                json.loads(stdout)
                return True, "valid JSON payload"
            except Exception as e:
                return False, f"JSON decode failed: {e}"

        elif atype == "custom_eval" and callable(exp):
            try:
                passed = bool(exp(execution_output))
                return passed, f"custom eval function returned: {passed}"
            except Exception as e:
                return False, f"custom eval raised: {e}"

        return False, f"unsupported assertion type: {atype}"

    @classmethod
    def verify_hypothesis(
        cls,
        hypothesis: RevisionHypothesis,
        candidate_runner: Callable[[Dict[str, Any]], Dict[str, Any]],
        tests: Optional[List[VerificationTest]] = None,
    ) -> Tuple[float, List[TestExecutionResult]]:
        """Run verification tests against a candidate revision and return (evidence_score, results)."""
        if tests is None:
            tests = cls.generate_verification_tests(hypothesis)

        results: List[TestExecutionResult] = []
        total_weight = sum(t.weight for t in tests) or 1.0
        weighted_score = 0.0

        for test in tests:
            t0 = time.time()
            try:
                out = candidate_runner(test.test_input)
            except Exception as err:
                out = {
                    "stdout": "",
                    "stderr": f"Runner exception: {err}",
                    "return_value": 1,
                }

            elapsed = time.time() - t0
            passed, evidence = cls.evaluate_test_assertion(test, out)
            score = 1.0 if passed else 0.0
            weighted_score += score * test.weight

            results.append(
                TestExecutionResult(
                    test_id=test.test_id,
                    hypothesis_id=test.hypothesis_id,
                    passed=passed,
                    actual_output=out,
                    evidence=evidence,
                    score=score,
                    execution_time_sec=elapsed,
                )
            )

        evidence_score = round(weighted_score / total_weight, 4)
        return evidence_score, results


@dataclass
class SkillRevisionNode:
    """A node in the SkillHEX revision tree."""

    node_id: str
    parent_id: Optional[str]
    skill_name: str
    skill_code: str
    hypothesis: Optional[RevisionHypothesis] = None
    visit_count: int = 0
    value: float = 0.0  # Cumulative evaluated score
    evidence_score: float = 0.0  # Dense verification evidence from self-verifier
    children_ids: List[str] = field(default_factory=list)
    depth: int = 0
    status: str = "pending"  # pending, verified, falsified, accepted, pruned
    metadata: Dict[str, Any] = field(default_factory=dict)

    def mean_value(self) -> float:
        return self.value / self.visit_count if self.visit_count > 0 else 0.0

    def ucb_score(
        self,
        parent_visits: int,
        exploration_weight: float = 1.414,
        evidence_weight: float = 0.5,
    ) -> float:
        """Compute SkillHEX UCB score combining mean reward, exploration bonus, and dense evidence score."""
        if self.visit_count == 0:
            # Unvisited nodes are prioritized, modulated by evidence and hypothesis confidence
            hyp_prior = self.hypothesis.confidence if self.hypothesis else 0.5
            return 100.0 + self.evidence_score + hyp_prior

        exploitation = self.mean_value()
        exploration = exploration_weight * math.sqrt(
            math.log(max(1, parent_visits)) / self.visit_count
        )
        evidence_term = evidence_weight * self.evidence_score
        return exploitation + exploration + evidence_term

    def to_dict(self) -> Dict[str, Any]:
        return {
            "node_id": self.node_id,
            "parent_id": self.parent_id,
            "skill_name": self.skill_name,
            "skill_code": self.skill_code,
            "hypothesis": self.hypothesis.to_dict() if self.hypothesis else None,
            "visit_count": self.visit_count,
            "value": self.value,
            "evidence_score": self.evidence_score,
            "children_ids": list(self.children_ids),
            "depth": self.depth,
            "status": self.status,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> SkillRevisionNode:
        hyp_data = data.get("hypothesis")
        hyp = RevisionHypothesis.from_dict(hyp_data) if hyp_data else None
        return cls(
            node_id=str(data.get("node_id", "")),
            parent_id=data.get("parent_id"),
            skill_name=str(data.get("skill_name", "")),
            skill_code=str(data.get("skill_code", "")),
            hypothesis=hyp,
            visit_count=int(data.get("visit_count", 0)),
            value=float(data.get("value", 0.0)),
            evidence_score=float(data.get("evidence_score", 0.0)),
            children_ids=list(data.get("children_ids", []) or []),
            depth=int(data.get("depth", 0)),
            status=str(data.get("status", "pending")),
            metadata=dict(data.get("metadata", {})),
        )


class SkillRevisionTree:
    """Search tree managing candidate skill revisions, hypotheses, and evidence scores."""

    def __init__(self, root_node: SkillRevisionNode) -> None:
        self.root_id = root_node.node_id
        self.nodes: Dict[str, SkillRevisionNode] = {root_node.node_id: root_node}

    @property
    def root(self) -> SkillRevisionNode:
        return self.nodes[self.root_id]

    def add_node(self, node: SkillRevisionNode) -> None:
        """Register a node in the tree and wire to parent children list."""
        self.nodes[node.node_id] = node
        if node.parent_id and node.parent_id in self.nodes:
            parent = self.nodes[node.parent_id]
            if node.node_id not in parent.children_ids:
                parent.children_ids.append(node.node_id)

    def select_node(
        self,
        exploration_weight: float = 1.414,
        evidence_weight: float = 0.5,
    ) -> SkillRevisionNode:
        """Traverse the tree using UCB1 + dense evidence to select an expandable or frontier node."""
        curr = self.root

        while curr.children_ids:
            # If any child is unvisited, pick it first
            unvisited = [
                self.nodes[cid]
                for cid in curr.children_ids
                if self.nodes[cid].visit_count == 0
            ]
            if unvisited:
                # Pick highest evidence score among unvisited
                return max(
                    unvisited,
                    key=lambda n: (
                        n.evidence_score,
                        n.hypothesis.confidence if n.hypothesis else 0.0,
                    ),
                )

            # Otherwise select best UCB child
            best_child = max(
                (self.nodes[cid] for cid in curr.children_ids),
                key=lambda child: child.ucb_score(
                    curr.visit_count, exploration_weight, evidence_weight
                ),
            )
            curr = best_child

        return curr

    def expand(
        self,
        node_id: str,
        hypotheses: Sequence[RevisionHypothesis],
        revision_generator: Callable[[str, RevisionHypothesis], str],
        verifier: Optional[HypothesisVerifier] = None,
        mock_runner_factory: Optional[
            Callable[[str], Callable[[Dict[str, Any]], Dict[str, Any]]]
        ] = None,
    ) -> List[SkillRevisionNode]:
        """Expand a node by generating revision branches for each candidate hypothesis."""
        parent = self.nodes.get(node_id)
        if not parent:
            raise KeyError(f"Node '{node_id}' not found in revision tree.")

        new_children: List[SkillRevisionNode] = []
        for i, hyp in enumerate(hypotheses, 1):
            child_id = f"{parent.node_id}_rev_{i}"
            revised_code = revision_generator(parent.skill_code, hyp)

            # Compute preliminary self-verification evidence score
            ev_score = 0.5
            if verifier and mock_runner_factory:
                runner = mock_runner_factory(revised_code)
                ev_score, _ = verifier.verify_hypothesis(hyp, runner)

            child_node = SkillRevisionNode(
                node_id=child_id,
                parent_id=parent.node_id,
                skill_name=parent.skill_name,
                skill_code=revised_code,
                hypothesis=hyp,
                visit_count=0,
                value=0.0,
                evidence_score=ev_score,
                children_ids=[],
                depth=parent.depth + 1,
                status="verified"
                if ev_score >= 0.7
                else ("falsified" if ev_score < 0.3 else "pending"),
            )
            self.add_node(child_node)
            new_children.append(child_node)

        return new_children

    def backpropagate(
        self,
        node_id: str,
        reward: float,
        evidence_score: Optional[float] = None,
    ) -> None:
        """Backpropagate execution reward and evidence up to the root."""
        curr_id: Optional[str] = node_id
        while curr_id and curr_id in self.nodes:
            node = self.nodes[curr_id]
            node.visit_count += 1
            node.value += reward
            if evidence_score is not None:
                # Moving update of evidence
                node.evidence_score = max(node.evidence_score, evidence_score)
            curr_id = node.parent_id

    def best_node(self) -> SkillRevisionNode:
        """Return the best revision node across the entire tree based on mean value and evidence."""
        candidates = [
            n for n in self.nodes.values() if n.visit_count > 0 or n.evidence_score > 0
        ]
        if not candidates:
            return self.root

        return max(
            candidates,
            key=lambda n: (
                n.mean_value(),
                n.evidence_score,
                -n.depth,  # prefer cleaner/shorter derivations on equal score
            ),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "root_id": self.root_id,
            "nodes": {nid: n.to_dict() for nid, n in self.nodes.items()},
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> SkillRevisionTree:
        nodes_dict = data.get("nodes", {})
        root_id = str(data.get("root_id", "root"))
        if root_id not in nodes_dict:
            # Create default dummy root
            root_node = SkillRevisionNode(
                node_id=root_id, parent_id=None, skill_name="default", skill_code=""
            )
            tree = cls(root_node)
        else:
            root_node = SkillRevisionNode.from_dict(nodes_dict[root_id])
            tree = cls(root_node)

        for nid, nd in nodes_dict.items():
            if nid != root_id:
                tree.nodes[nid] = SkillRevisionNode.from_dict(nd)
        return tree

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_json(cls, json_str: str) -> SkillRevisionTree:
        return cls.from_dict(json.loads(json_str))


def generate_hypotheses_from_failure(
    skill_name: str,
    failure_trace: Dict[str, Any],
) -> List[RevisionHypothesis]:
    """Heuristic / pattern-based hypothesis generator from a recorded failure trace."""
    error_msg = str(failure_trace.get("error", failure_trace.get("stderr", "")))
    hypotheses: List[RevisionHypothesis] = []

    if "syntax" in error_msg.lower() or "ast" in error_msg.lower():
        hypotheses.append(
            RevisionHypothesis(
                hypothesis_id="hyp_syntax_ast",
                description="Syntax or AST generation error in target code.",
                failure_cause_category="logic_error",
                confidence=0.85,
                proposed_fix="Add AST parse validation before output.",
                falsification_condition="AST parse passes without syntax error.",
            )
        )

    if "keyerror" in error_msg.lower() or "missing" in error_msg.lower():
        hypotheses.append(
            RevisionHypothesis(
                hypothesis_id="hyp_missing_key",
                description="Required input key or parameter missing from payload.",
                failure_cause_category="precondition_missing",
                confidence=0.8,
                proposed_fix="Add default fallback for missing payload keys.",
                falsification_condition="Payload missing key runs with default value.",
            )
        )

    if (
        "timeout" in error_msg.lower()
        or "timed out" in error_msg.lower()
        or "slow" in error_msg.lower()
    ):
        hypotheses.append(
            RevisionHypothesis(
                hypothesis_id="hyp_timeout",
                description="Execution time exceeded budget.",
                failure_cause_category="boundary_case",
                confidence=0.7,
                proposed_fix="Optimize inner loop and add early exit.",
                falsification_condition="Execution finishes under timeout limit.",
            )
        )

    # General fallback hypothesis
    if not hypotheses:
        hypotheses.append(
            RevisionHypothesis(
                hypothesis_id="hyp_general_robustness",
                description=f"General edge-case failure in {skill_name}: {error_msg[:100]}",
                failure_cause_category="boundary_case",
                confidence=0.6,
                proposed_fix="Wrap with try-except defensive error handling.",
                falsification_condition="Function returns structured error rather than crashing.",
            )
        )

    return hypotheses


class SkillHEXOptimizer:
    """Orchestrator for SkillHEX: Hypothesis-driven exploration and exploitation over revision trees."""

    def __init__(
        self,
        verifier: Optional[HypothesisVerifier] = None,
        exploration_weight: float = 1.414,
        evidence_weight: float = 0.5,
    ) -> None:
        self.verifier = verifier or HypothesisVerifier()
        self.exploration_weight = exploration_weight
        self.evidence_weight = evidence_weight

    def run_tree_search(
        self,
        skill_name: str,
        initial_skill_code: str,
        hypothesis_generator: Callable[[str, Dict[str, Any]], List[RevisionHypothesis]],
        revision_generator: Callable[[str, RevisionHypothesis], str],
        evaluator: Callable[[str], float],
        *,
        max_iterations: int = 5,
        target_score_threshold: float = 0.85,
        mock_runner_factory: Optional[
            Callable[[str], Callable[[Dict[str, Any]], Dict[str, Any]]]
        ] = None,
    ) -> Dict[str, Any]:
        """Run evidence-guided tree search over skill revision branches.

        Args:
            skill_name: Name of skill being optimized.
            initial_skill_code: Baseline skill code.
            hypothesis_generator: Callable producing candidate failure hypotheses.
            revision_generator: Callable producing revised code given a hypothesis.
            evaluator: Evaluator scoring a candidate skill code string [0.0..1.0].
            max_iterations: Maximum tree search iterations.
            target_score_threshold: Score threshold for early convergence.
            mock_runner_factory: Factory creating runners for self-verifier tests.

        Returns:
            Dict containing best_node, best_code, best_score, total_iterations, converged, tree.
        """
        # Initialize root node
        initial_score = evaluator(initial_skill_code)
        root = SkillRevisionNode(
            node_id="node_0_root",
            parent_id=None,
            skill_name=skill_name,
            skill_code=initial_skill_code,
            visit_count=1,
            value=initial_score,
            evidence_score=0.5,
            depth=0,
            status="verified" if initial_score >= target_score_threshold else "pending",
        )
        tree = SkillRevisionTree(root)

        if initial_score >= target_score_threshold:
            return {
                "converged": True,
                "best_score": initial_score,
                "best_code": initial_skill_code,
                "best_node": root.to_dict(),
                "total_iterations": 0,
                "tree": tree.to_dict(),
            }

        converged = False
        best_overall_score = initial_score

        for iteration in range(1, max_iterations + 1):
            # 1. Selection
            selected_node = tree.select_node(
                exploration_weight=self.exploration_weight,
                evidence_weight=self.evidence_weight,
            )

            # 2. Generate hypotheses for selected node
            hypotheses = hypothesis_generator(
                skill_name,
                {
                    "error": f"Evaluation score {selected_node.mean_value():.2f} below threshold {target_score_threshold}"
                },
            )

            # 3. Expansion
            new_nodes = tree.expand(
                node_id=selected_node.node_id,
                hypotheses=hypotheses,
                revision_generator=revision_generator,
                verifier=self.verifier,
                mock_runner_factory=mock_runner_factory,
            )

            # 4. Evaluation & Backpropagation
            for child in new_nodes:
                score = evaluator(child.skill_code)
                tree.backpropagate(
                    child.node_id, reward=score, evidence_score=child.evidence_score
                )
                if score > best_overall_score:
                    best_overall_score = score
                if score >= target_score_threshold:
                    child.status = "accepted"
                    converged = True
                    break

            if converged:
                break

        best = tree.best_node()
        return {
            "converged": converged,
            "best_score": best.mean_value(),
            "best_code": best.skill_code,
            "best_node": best.to_dict(),
            "total_iterations": iteration,
            "tree": tree.to_dict(),
        }
