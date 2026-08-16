# -*- coding: utf-8 -*-
"""AUTOHARNESS: Automatic test harness synthesis for agent self-improvement (Issue #2250, Slice A #2516).

Synthesizes executable evaluation harness specifications from a skill's declared
capabilities and contracts, enabling dynamic, scalable evaluation without requiring
hand-authored test criteria for every iteration.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional, Union

logger = logging.getLogger(__name__)

__all__ = [
    "HARNESS_SPEC_JSON_SCHEMA",
    "HARNESS_SYNTHESIS_SYSTEM_PROMPT",
    "AssertionResult",
    "CycleEvaluationResult",
    "HarnessAssertion",
    "HarnessDiagnosticReport",
    "HarnessSpec",
    "TestCaseResult",
    "TestCaseSpec",
    "build_harness_synthesis_prompt",
    "evaluate_assertion",
    "evaluate_improvement_cycle",
    "parse_skill_capabilities",
    "run_autoharness_loop",
    "run_harness",
    "synthesize_harness_spec",
]

HARNESS_SPEC_JSON_SCHEMA: Dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "HarnessSpec",
    "type": "object",
    "required": [
        "skill_name",
        "skill_category",
        "claimed_capabilities",
        "test_cases",
        "passing_threshold",
    ],
    "properties": {
        "skill_name": {"type": "string"},
        "skill_category": {"type": "string"},
        "claimed_capabilities": {
            "type": "array",
            "items": {"type": "string"},
        },
        "passing_threshold": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 1.0,
            "default": 0.8,
        },
        "metadata": {"type": "object"},
        "test_cases": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["test_id", "name", "input_data", "assertions"],
                "properties": {
                    "test_id": {"type": "string"},
                    "name": {"type": "string"},
                    "input_data": {"type": "object"},
                    "expected_behavior": {"type": "string"},
                    "timeout_sec": {"type": "number", "default": 30.0},
                    "assertions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["assertion_type", "target", "expected"],
                            "properties": {
                                "assertion_type": {
                                    "type": "string",
                                    "enum": [
                                        "output_contains",
                                        "regex_match",
                                        "exit_code",
                                        "json_valid",
                                        "custom_eval",
                                        "execution_time_under",
                                    ],
                                },
                                "target": {
                                    "type": "string",
                                    "enum": [
                                        "stdout",
                                        "stderr",
                                        "return_value",
                                        "artifacts",
                                        "state",
                                    ],
                                },
                                "expected": {},
                                "weight": {
                                    "type": "number",
                                    "minimum": 0.0,
                                    "default": 1.0,
                                },
                                "description": {"type": "string"},
                            },
                        },
                    },
                },
            },
        },
    },
}

HARNESS_SYNTHESIS_SYSTEM_PROMPT: str = """\
You are AUTOHARNESS, an expert evaluation synthesis engine for AI agent skills and tools.
Given a skill's declared capabilities, contracts, and usage patterns, your goal is to
generate a rigorous, executable test harness specification conforming to the HarnessSpec schema.

Your generated test cases must:
1. Cover each claimed capability with boundary, nominal, and error-handling cases.
2. Define unambiguous, deterministic assertions (e.g. exit_code, output_contains, json_valid, regex_match).
3. Assign appropriate scoring weights so core functionality dominates cosmetic criteria.
4. Set reasonable execution timeouts and passing thresholds (typically >= 0.8).
"""


@dataclass
class HarnessAssertion:
    """A single graded assertion within a test case."""

    assertion_type: (
        str  # "output_contains", "regex_match", "exit_code", "json_valid", etc.
    )
    target: str  # "stdout", "stderr", "return_value", "artifacts", "state"
    expected: Any
    weight: float = 1.0
    description: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> HarnessAssertion:
        return cls(
            assertion_type=str(d.get("assertion_type", "output_contains")),
            target=str(d.get("target", "stdout")),
            expected=d.get("expected"),
            weight=float(d.get("weight", 1.0)),
            description=str(d.get("description", "")),
        )


@dataclass
class TestCaseSpec:
    """A synthesized test case testing a specific capability."""

    __test__ = False

    test_id: str
    name: str
    input_data: Dict[str, Any] = field(default_factory=dict)
    expected_behavior: str = ""
    assertions: List[HarnessAssertion] = field(default_factory=list)
    timeout_sec: float = 30.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "test_id": self.test_id,
            "name": self.name,
            "input_data": self.input_data,
            "expected_behavior": self.expected_behavior,
            "timeout_sec": self.timeout_sec,
            "assertions": [a.to_dict() for a in self.assertions],
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> TestCaseSpec:
        raw_assertions = d.get("assertions", []) or []
        assertions = [
            HarnessAssertion.from_dict(a) if isinstance(a, dict) else a
            for a in raw_assertions
        ]
        return cls(
            test_id=str(d.get("test_id", "")),
            name=str(d.get("name", "")),
            input_data=dict(d.get("input_data", {}) or {}),
            expected_behavior=str(d.get("expected_behavior", "")),
            assertions=assertions,
            timeout_sec=float(d.get("timeout_sec", 30.0)),
        )


@dataclass
class HarnessSpec:
    """Complete synthesized evaluation harness for a skill."""

    skill_name: str
    skill_category: str
    claimed_capabilities: List[str] = field(default_factory=list)
    test_cases: List[TestCaseSpec] = field(default_factory=list)
    passing_threshold: float = 0.8
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "skill_name": self.skill_name,
            "skill_category": self.skill_category,
            "claimed_capabilities": list(self.claimed_capabilities),
            "test_cases": [tc.to_dict() for tc in self.test_cases],
            "passing_threshold": self.passing_threshold,
            "metadata": dict(self.metadata),
        }

    def to_json(self, indent: Optional[int] = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> HarnessSpec:
        raw_cases = d.get("test_cases", []) or []
        cases = [
            TestCaseSpec.from_dict(tc) if isinstance(tc, dict) else tc
            for tc in raw_cases
        ]
        return cls(
            skill_name=str(d.get("skill_name", "unnamed_skill")),
            skill_category=str(d.get("skill_category", "general")),
            claimed_capabilities=[
                str(c) for c in d.get("claimed_capabilities", []) or []
            ],
            test_cases=cases,
            passing_threshold=float(d.get("passing_threshold", 0.8)),
            metadata=dict(d.get("metadata", {}) or {}),
        )

    @classmethod
    def from_json(cls, json_str: str) -> HarnessSpec:
        data = json.loads(json_str)
        return cls.from_dict(data)


def parse_skill_capabilities(
    skill_def: Union[Dict[str, Any], str],
) -> Dict[str, Any]:
    """Extract skill name, category, description, and claimed capabilities from dict or markdown."""
    if isinstance(skill_def, dict):
        name = str(
            skill_def.get("name") or skill_def.get("skill_name") or "unnamed_skill"
        )
        category = str(
            skill_def.get("category") or skill_def.get("skill_category") or "general"
        )
        description = str(skill_def.get("description", ""))
        capabilities = list(
            skill_def.get("capabilities") or skill_def.get("claimed_capabilities") or []
        )
        if not capabilities and description:
            capabilities = [
                s.strip()
                for s in re.split(r"[;\n\.]+", description)
                if len(s.strip()) > 5
            ]
        return {
            "name": name,
            "category": category,
            "description": description,
            "capabilities": capabilities,
            "metadata": skill_def.get("metadata", {}),
        }

    text = str(skill_def).strip()
    name = "unnamed_skill"
    category = "general"
    description = ""
    capabilities: List[str] = []

    # Check for YAML frontmatter
    fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, re.DOTALL)
    body = text
    if fm_match:
        fm_content, body = fm_match.group(1), fm_match.group(2)
        for line in fm_content.splitlines():
            line = line.strip()
            if line.startswith("name:"):
                name = line.split(":", 1)[1].strip().strip("\"'")
            elif line.startswith("description:"):
                description = line.split(":", 1)[1].strip().strip("\"'")
            elif line.startswith("category:"):
                category = line.split(":", 1)[1].strip().strip("\"'")

    # Extract title if missing name
    if name == "unnamed_skill":
        title_match = re.search(r"^#\s+(.+)$", body, re.MULTILINE)
        if title_match:
            name = title_match.group(1).strip().lower().replace(" ", "_")

    # Extract capability bullets
    cap_patterns = [
        r"(?i)capabilities:?\s*\n((?:\s*[-*]\s+.+\n?)+)",
        r"(?i)features:?\s*\n((?:\s*[-*]\s+.+\n?)+)",
        r"(?i)supported operations:?\s*\n((?:\s*[-*]\s+.+\n?)+)",
    ]
    for cp in cap_patterns:
        m = re.search(cp, body)
        if m:
            bullet_block = m.group(1)
            for line in bullet_block.splitlines():
                clean_line = re.sub(r"^\s*[-*]\s+", "", line).strip()
                if clean_line:
                    capabilities.append(clean_line)
            break

    if not capabilities:
        for line in body.splitlines():
            clean = line.strip()
            if clean.startswith(("- ", "* ")) and len(clean) > 8:
                capabilities.append(clean[2:].strip())

    if not capabilities and description:
        capabilities = [
            s.strip() for s in re.split(r"[;\n\.]+", description) if len(s.strip()) > 5
        ]

    # Category heuristic if still general
    lower_text = text.lower()
    if category == "general":
        if any(k in lower_text for k in ("code", "refactor", "ast", "python", "fn")):
            category = "coding"
        elif any(
            k in lower_text for k in ("data", "json", "csv", "transform", "parse")
        ):
            category = "data_processing"
        elif any(
            k in lower_text for k in ("search", "retrieve", "query", "vector", "bm25")
        ):
            category = "search_retrieval"
        elif any(
            k in lower_text for k in ("workflow", "pipeline", "cron", "orchestrat")
        ):
            category = "workflow_automation"

    return {
        "name": name,
        "category": category,
        "description": description,
        "capabilities": capabilities,
        "metadata": {},
    }


def build_harness_synthesis_prompt(
    skill_def: Union[Dict[str, Any], str],
) -> str:
    """Build the prompt for synthesizing an evaluation harness spec from a skill definition."""
    parsed = parse_skill_capabilities(skill_def)
    caps_formatted = (
        "\n".join(f"- {cap}" for cap in parsed["capabilities"])
        or "- General skill execution"
    )

    return f"""\
Skill Name: {parsed["name"]}
Category: {parsed["category"]}
Description: {parsed["description"] or "None"}

Declared Capabilities:
{caps_formatted}

Task:
Synthesize an evaluation harness specification in JSON conforming to the HarnessSpec schema.
Include test cases for nominal execution, boundary conditions, and error states.
"""


def _synthesize_category_test_cases(
    skill_name: str,
    category: str,
    capabilities: List[str],
) -> List[TestCaseSpec]:
    """Generate deterministic test case specifications for standard skill categories."""
    test_cases: List[TestCaseSpec] = []
    caps = capabilities or ["execute basic operation"]

    if category == "coding":
        for i, cap in enumerate(caps, 1):
            test_cases.append(
                TestCaseSpec(
                    test_id=f"test_coding_{i}_nominal",
                    name=f"Verify {cap} on valid input",
                    input_data={"mode": "nominal", "capability": cap},
                    expected_behavior=f"Skill executes {cap} cleanly and returns valid output or zero exit code.",
                    assertions=[
                        HarnessAssertion(
                            assertion_type="exit_code",
                            target="return_value",
                            expected=0,
                            weight=1.0,
                            description="Successful completion",
                        ),
                        HarnessAssertion(
                            assertion_type="json_valid",
                            target="stdout",
                            expected=True,
                            weight=0.8,
                            description="Output contains structured JSON payload",
                        ),
                    ],
                    timeout_sec=20.0,
                )
            )
        # Add boundary/error test case
        test_cases.append(
            TestCaseSpec(
                test_id=f"test_coding_{len(caps) + 1}_error_boundary",
                name="Verify error handling on malformed input",
                input_data={"mode": "invalid", "payload": None},
                expected_behavior="Skill gracefully handles invalid input without uncaught exceptions.",
                assertions=[
                    HarnessAssertion(
                        assertion_type="output_contains",
                        target="stderr",
                        expected="error",
                        weight=0.5,
                        description="Reports error message on invalid input",
                    )
                ],
                timeout_sec=15.0,
            )
        )
    elif category == "data_processing":
        for i, cap in enumerate(caps, 1):
            test_cases.append(
                TestCaseSpec(
                    test_id=f"test_data_{i}_transform",
                    name=f"Validate data transformation for {cap}",
                    input_data={"operation": cap, "records_count": 10},
                    expected_behavior=f"Data pipeline transforms input correctly fulfilling {cap}.",
                    assertions=[
                        HarnessAssertion(
                            assertion_type="json_valid",
                            target="stdout",
                            expected=True,
                            weight=1.0,
                            description="Outputs valid transformed JSON",
                        ),
                        HarnessAssertion(
                            assertion_type="execution_time_under",
                            target="state",
                            expected=5.0,
                            weight=0.5,
                            description="Completes within expected latency bound",
                        ),
                    ],
                    timeout_sec=15.0,
                )
            )
    elif category == "search_retrieval":
        for i, cap in enumerate(caps, 1):
            test_cases.append(
                TestCaseSpec(
                    test_id=f"test_search_{i}_retrieval",
                    name=f"Verify retrieval accuracy for {cap}",
                    input_data={"query": f"test {cap}", "top_k": 5},
                    expected_behavior=f"Retrieves relevant results according to {cap}.",
                    assertions=[
                        HarnessAssertion(
                            assertion_type="output_contains",
                            target="stdout",
                            expected="results",
                            weight=1.0,
                            description="Contains results field",
                        ),
                        HarnessAssertion(
                            assertion_type="json_valid",
                            target="stdout",
                            expected=True,
                            weight=0.7,
                            description="Structured retrieval results format",
                        ),
                    ],
                    timeout_sec=20.0,
                )
            )
    elif category == "workflow_automation":
        for i, cap in enumerate(caps, 1):
            test_cases.append(
                TestCaseSpec(
                    test_id=f"test_workflow_{i}_step",
                    name=f"Verify workflow step {cap}",
                    input_data={"action": cap, "step_id": i},
                    expected_behavior=f"Executes workflow action {cap} with proper status tracking.",
                    assertions=[
                        HarnessAssertion(
                            assertion_type="exit_code",
                            target="return_value",
                            expected=0,
                            weight=1.0,
                            description="Workflow step succeeded",
                        ),
                        HarnessAssertion(
                            assertion_type="output_contains",
                            target="stdout",
                            expected="status",
                            weight=0.8,
                            description="Emits status notification",
                        ),
                    ],
                    timeout_sec=30.0,
                )
            )
    else:  # general fallback
        for i, cap in enumerate(caps, 1):
            test_cases.append(
                TestCaseSpec(
                    test_id=f"test_general_{i}",
                    name=f"Execute {cap}",
                    input_data={"task": cap},
                    expected_behavior=f"Performs {cap} successfully.",
                    assertions=[
                        HarnessAssertion(
                            assertion_type="exit_code",
                            target="return_value",
                            expected=0,
                            weight=1.0,
                            description="Returns success status",
                        )
                    ],
                    timeout_sec=30.0,
                )
            )

    return test_cases


def synthesize_harness_spec(
    skill_def: Union[Dict[str, Any], str, Path],
    default_category: Optional[str] = None,
    llm_synthesizer: Optional[Callable[[str], str]] = None,
    passing_threshold: float = 0.8,
    context: Optional[Dict[str, Any]] = None,
) -> HarnessSpec:
    """Synthesize an executable HarnessSpec from a skill's declared capabilities (Issue #2250, Slice A #2516)."""
    parsed = parse_skill_capabilities(skill_def)
    category = (
        default_category if default_category else parsed.get("category", "general")
    )
    capabilities = parsed.get("capabilities", [])

    if llm_synthesizer is not None:
        try:
            prompt = build_harness_synthesis_prompt(skill_def, context=context)
            raw_response = llm_synthesizer(prompt)
            # Try parsing JSON payload from LLM
            clean_json = raw_response.strip()
            if "```json" in clean_json:
                clean_json = (
                    clean_json.split("```json", 1)[1].split("```", 1)[0].strip()
                )
            elif "```" in clean_json:
                clean_json = clean_json.split("```", 1)[1].split("```", 1)[0].strip()
            spec_data = json.loads(clean_json)
            if isinstance(spec_data, dict) and "test_cases" in spec_data:
                spec = HarnessSpec.from_dict(spec_data)
                spec.passing_threshold = passing_threshold
                return spec
        except Exception as e:
            logger.warning(
                "LLM harness synthesis failed, falling back to heuristic: %s", e
            )

    test_cases = _synthesize_category_test_cases(
        skill_name=parsed["name"],
        category=category,
        capabilities=capabilities,
    )

    return HarnessSpec(
        skill_name=parsed["name"],
        skill_category=category,
        claimed_capabilities=capabilities,
        test_cases=test_cases,
        passing_threshold=passing_threshold,
        metadata={
            "description": parsed.get("description", ""),
            "synthesized_by": "AUTOHARNESS",
            "capabilities_count": len(capabilities),
            "test_cases_count": len(test_cases),
        },
    )


# ──────────────────────────────────────────────────────────────────────
# Slice B: Run improved-skill against synthesized harness + diagnostics (#2517)
# ──────────────────────────────────────────────────────────────────────


@dataclass
class AssertionResult:
    """Outcome of an assertion evaluation."""

    assertion: HarnessAssertion
    passed: bool
    actual: Any = None
    message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "assertion": self.assertion.to_dict(),
            "passed": self.passed,
            "actual": self.actual,
            "message": self.message,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> AssertionResult:
        raw_a = d.get("assertion", {}) or {}
        a = HarnessAssertion.from_dict(raw_a) if isinstance(raw_a, dict) else raw_a
        return cls(
            assertion=a,
            passed=bool(d.get("passed", False)),
            actual=d.get("actual"),
            message=str(d.get("message", "")),
        )


@dataclass
class TestCaseResult:
    """Outcome of a single test case execution against a skill."""

    __test__ = False

    test_id: str
    name: str
    passed: bool
    score: float
    assertion_results: List[AssertionResult] = field(default_factory=list)
    execution_time_sec: float = 0.0
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "test_id": self.test_id,
            "name": self.name,
            "passed": self.passed,
            "score": round(self.score, 4),
            "execution_time_sec": round(self.execution_time_sec, 4),
            "error": self.error,
            "assertion_results": [ar.to_dict() for ar in self.assertion_results],
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> TestCaseResult:
        raw_ar = d.get("assertion_results", []) or []
        ar_list = [
            AssertionResult.from_dict(ar) if isinstance(ar, dict) else ar
            for ar in raw_ar
        ]
        return cls(
            test_id=str(d.get("test_id", "")),
            name=str(d.get("name", "")),
            passed=bool(d.get("passed", False)),
            score=float(d.get("score", 0.0)),
            assertion_results=ar_list,
            execution_time_sec=float(d.get("execution_time_sec", 0.0)),
            error=d.get("error"),
        )


@dataclass
class HarnessDiagnosticReport:
    """Structured diagnostic report emitted when running a skill against a harness."""

    skill_name: str
    skill_category: str
    total_cases: int
    passed_cases: int
    failed_cases: int
    overall_score: float
    passed: bool
    test_case_results: List[TestCaseResult] = field(default_factory=list)
    prior_score: Optional[float] = None
    score_delta: Optional[float] = None
    per_criterion_diagnostics: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "skill_name": self.skill_name,
            "skill_category": self.skill_category,
            "total_cases": self.total_cases,
            "passed_cases": self.passed_cases,
            "failed_cases": self.failed_cases,
            "overall_score": round(self.overall_score, 4),
            "passed": self.passed,
            "prior_score": (
                round(self.prior_score, 4) if self.prior_score is not None else None
            ),
            "score_delta": (
                round(self.score_delta, 4) if self.score_delta is not None else None
            ),
            "per_criterion_diagnostics": self.per_criterion_diagnostics,
            "test_case_results": [tcr.to_dict() for tcr in self.test_case_results],
            "metadata": dict(self.metadata),
        }

    def to_json(self, indent: Optional[int] = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> HarnessDiagnosticReport:
        raw_tcr = d.get("test_case_results", []) or []
        tcr_list = [
            TestCaseResult.from_dict(tcr) if isinstance(tcr, dict) else tcr
            for tcr in raw_tcr
        ]
        return cls(
            skill_name=str(d.get("skill_name", "unnamed_skill")),
            skill_category=str(d.get("skill_category", "general")),
            total_cases=int(d.get("total_cases", len(tcr_list))),
            passed_cases=int(d.get("passed_cases", 0)),
            failed_cases=int(d.get("failed_cases", 0)),
            overall_score=float(d.get("overall_score", 0.0)),
            passed=bool(d.get("passed", False)),
            test_case_results=tcr_list,
            prior_score=(
                float(d["prior_score"]) if d.get("prior_score") is not None else None
            ),
            score_delta=(
                float(d["score_delta"]) if d.get("score_delta") is not None else None
            ),
            per_criterion_diagnostics=dict(
                d.get("per_criterion_diagnostics", {}) or {}
            ),
            metadata=dict(d.get("metadata", {}) or {}),
        )

    @classmethod
    def from_json(cls, json_str: str) -> HarnessDiagnosticReport:
        data = json.loads(json_str)
        return cls.from_dict(data)


def evaluate_assertion(
    assertion: HarnessAssertion,
    execution_output: Dict[str, Any],
) -> AssertionResult:
    """Evaluate a single test assertion against execution outputs."""
    target_val = execution_output.get(assertion.target)
    atype = assertion.assertion_type
    expected = assertion.expected

    if atype == "exit_code":
        try:
            actual_int = int(target_val) if target_val is not None else -1
            passed = actual_int == int(expected)
            msg = (
                f"Exit code matches ({actual_int})"
                if passed
                else f"Exit code mismatch: expected {expected}, got {actual_int}"
            )
            return AssertionResult(
                assertion=assertion,
                passed=passed,
                actual=actual_int,
                message=msg,
            )
        except (ValueError, TypeError) as e:
            return AssertionResult(
                assertion=assertion,
                passed=False,
                actual=target_val,
                message=f"Invalid exit code value: {e}",
            )

    elif atype == "output_contains":
        actual_str = str(target_val or "")
        exp_str = str(expected or "")
        passed = exp_str.lower() in actual_str.lower()
        msg = (
            f"Output contains '{exp_str}'"
            if passed
            else f"Output did not contain '{exp_str}'"
        )
        return AssertionResult(
            assertion=assertion,
            passed=passed,
            actual=actual_str[:200],
            message=msg,
        )

    elif atype == "regex_match":
        actual_str = str(target_val or "")
        pattern_str = str(expected or "")
        passed = bool(re.search(pattern_str, actual_str))
        msg = (
            f"Output matched pattern '{pattern_str}'"
            if passed
            else f"Output failed regex match '{pattern_str}'"
        )
        return AssertionResult(
            assertion=assertion,
            passed=passed,
            actual=actual_str[:200],
            message=msg,
        )

    elif atype == "json_valid":
        actual_str = str(target_val or "")
        try:
            parsed = (
                target_val
                if isinstance(target_val, (dict, list))
                else json.loads(actual_str)
            )
            passed = True if expected is True else False
            msg = "Valid JSON payload"
            return AssertionResult(
                assertion=assertion,
                passed=passed,
                actual=type(parsed).__name__,
                message=msg,
            )
        except (json.JSONDecodeError, TypeError) as e:
            passed = False if expected is True else True
            return AssertionResult(
                assertion=assertion,
                passed=passed,
                actual=actual_str[:100],
                message=f"JSON validation failed: {e}",
            )

    elif atype == "execution_time_under":
        try:
            actual_time = (
                float(target_val)
                if target_val is not None
                else float(execution_output.get("execution_time_sec", 999.0))
            )
            passed = actual_time <= float(expected)
            msg = (
                f"Execution time {actual_time:.2f}s <= {expected}s bound"
                if passed
                else f"Latency bound exceeded: {actual_time:.2f}s > {expected}s"
            )
            return AssertionResult(
                assertion=assertion,
                passed=passed,
                actual=actual_time,
                message=msg,
            )
        except (ValueError, TypeError):
            return AssertionResult(
                assertion=assertion,
                passed=False,
                actual=target_val,
                message="Invalid execution time",
            )

    elif atype == "custom_eval":
        if callable(expected):
            try:
                passed = bool(expected(target_val))
                msg = (
                    "Custom evaluation passed" if passed else "Custom evaluation failed"
                )
            except Exception as e:
                passed = False
                msg = f"Custom evaluation error: {e}"
        else:
            passed = target_val == expected
            msg = (
                "Value matches expected"
                if passed
                else f"Value mismatch: expected {expected}, got {target_val}"
            )
        return AssertionResult(
            assertion=assertion,
            passed=passed,
            actual=target_val,
            message=msg,
        )

    # Fallback equality
    passed = target_val == expected
    return AssertionResult(
        assertion=assertion,
        passed=passed,
        actual=target_val,
        message="Equality check",
    )


def run_harness(
    harness_spec: HarnessSpec,
    skill_runner: Callable[[Dict[str, Any]], Dict[str, Any]],
    prior_report: Optional[HarnessDiagnosticReport] = None,
) -> HarnessDiagnosticReport:
    """Execute a skill against a synthesized HarnessSpec and emit structured diagnostics (Slice B #2517)."""
    test_case_results: List[TestCaseResult] = []
    criterion_stats: Dict[str, Dict[str, int]] = {}

    for tc in harness_spec.test_cases:
        t_start = time.time()
        err_msg: Optional[str] = None
        exec_output: Dict[str, Any] = {}
        try:
            exec_output = skill_runner(tc.input_data)
        except Exception as e:
            err_msg = str(e)
            logger.warning("Error executing test case %s: %s", tc.test_id, err_msg)
            exec_output = {
                "stdout": "",
                "stderr": err_msg,
                "return_value": -1,
                "state": {},
            }
        t_elapsed = time.time() - t_start

        assertion_results: List[AssertionResult] = []
        total_weight = 0.0
        passed_weight = 0.0

        for assertion in tc.assertions:
            total_weight += assertion.weight
            crit = assertion.assertion_type
            if crit not in criterion_stats:
                criterion_stats[crit] = {"total": 0, "passed": 0}
            criterion_stats[crit]["total"] += 1

            if err_msg and assertion.target != "stderr":
                ar = AssertionResult(
                    assertion=assertion,
                    passed=False,
                    actual=None,
                    message=f"Execution error: {err_msg}",
                )
            else:
                ar = evaluate_assertion(assertion, exec_output)

            if ar.passed:
                passed_weight += assertion.weight
                criterion_stats[crit]["passed"] += 1
            assertion_results.append(ar)

        tc_score = (passed_weight / total_weight) if total_weight > 0 else 0.0
        tc_passed = tc_score >= 0.99 and err_msg is None

        test_case_results.append(
            TestCaseResult(
                test_id=tc.test_id,
                name=tc.name,
                passed=tc_passed,
                score=tc_score,
                assertion_results=assertion_results,
                execution_time_sec=t_elapsed,
                error=err_msg,
            )
        )

    total_cases = len(test_case_results)
    passed_cases = sum(1 for r in test_case_results if r.passed)
    failed_cases = total_cases - passed_cases
    overall_score = (
        sum(r.score for r in test_case_results) / total_cases
        if total_cases > 0
        else 0.0
    )
    overall_passed = overall_score >= harness_spec.passing_threshold

    prior_score = prior_report.overall_score if prior_report else None
    score_delta = (
        round(overall_score - prior_score, 4) if prior_score is not None else None
    )

    per_crit_diag: Dict[str, Any] = {}
    for crit, counts in criterion_stats.items():
        tot = counts["total"]
        pas = counts["passed"]
        pct = round((pas / tot) * 100, 1) if tot > 0 else 0.0
        per_crit_diag[crit] = {
            "total_assertions": tot,
            "passed_assertions": pas,
            "pass_rate_pct": pct,
        }

    return HarnessDiagnosticReport(
        skill_name=harness_spec.skill_name,
        skill_category=harness_spec.skill_category,
        total_cases=total_cases,
        passed_cases=passed_cases,
        failed_cases=failed_cases,
        overall_score=overall_score,
        passed=overall_passed,
        test_case_results=test_case_results,
        prior_score=prior_score,
        score_delta=score_delta,
        per_criterion_diagnostics=per_crit_diag,
        metadata={
            "passing_threshold": harness_spec.passing_threshold,
            "synthesized_cases_count": len(harness_spec.test_cases),
        },
    )


@dataclass
class CycleEvaluationResult:
    """Outcome of grading an improvement candidate in a self-improvement cycle (Slice C)."""

    cycle_index: int
    skill_name: str
    harness_spec: HarnessSpec
    candidate_report: HarnessDiagnosticReport
    baseline_report: Optional[HarnessDiagnosticReport] = None
    accepted: bool = False
    score_delta: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "cycle_index": self.cycle_index,
            "skill_name": self.skill_name,
            "harness_spec": self.harness_spec.to_dict(),
            "candidate_report": self.candidate_report.to_dict(),
            "baseline_report": (
                self.baseline_report.to_dict() if self.baseline_report else None
            ),
            "accepted": self.accepted,
            "score_delta": self.score_delta,
            "metadata": self.metadata,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, default=str)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> CycleEvaluationResult:
        base_data = data.get("baseline_report")
        base_report = (
            HarnessDiagnosticReport.from_dict(base_data) if base_data else None
        )
        return cls(
            cycle_index=int(data.get("cycle_index", 1)),
            skill_name=str(data.get("skill_name", "")),
            harness_spec=HarnessSpec.from_dict(dict(data.get("harness_spec", {}))),
            candidate_report=HarnessDiagnosticReport.from_dict(
                dict(data.get("candidate_report", {}))
            ),
            baseline_report=base_report,
            accepted=bool(data.get("accepted", False)),
            score_delta=(
                float(data["score_delta"])
                if data.get("score_delta") is not None
                else None
            ),
            metadata=dict(data.get("metadata", {})),
        )

    @classmethod
    def from_json(cls, json_str: str) -> CycleEvaluationResult:
        return cls.from_dict(json.loads(json_str))


def evaluate_improvement_cycle(
    skill_dict: Union[Dict[str, Any], str, Path],
    candidate_runner: Callable[[Dict[str, Any]], Dict[str, Any]],
    baseline_runner: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
    *,
    cycle_index: int = 1,
    llm_synthesizer: Optional[Callable[[str], str]] = None,
    passing_threshold: float = 0.8,
    require_non_negative_delta: bool = True,
    context: Optional[Dict[str, Any]] = None,
) -> CycleEvaluationResult:
    """Run one evaluation cycle by synthesizing a fresh harness and grading the candidate.

    1. Synthesizes fresh criteria (HarnessSpec) for this cycle from the skill definition.
    2. Runs the baseline runner if provided to establish prior diagnostic baseline.
    3. Runs the candidate runner against the freshly synthesized harness.
    4. Computes pass/fail acceptance based on threshold and score delta.
    """
    # 1. Synthesis fresh criteria
    spec = synthesize_harness_spec(
        skill_def=skill_dict,
        llm_synthesizer=llm_synthesizer,
        passing_threshold=passing_threshold,
        context=context,
    )

    # 2. Baseline run if available
    baseline_report = None
    if baseline_runner is not None:
        baseline_report = run_harness(spec, baseline_runner)

    # 3. Candidate evaluation against freshly synthesized criteria
    candidate_report = run_harness(spec, candidate_runner, prior_report=baseline_report)

    # 4. Determine acceptance
    score_delta = candidate_report.score_delta
    passed = candidate_report.passed
    if require_non_negative_delta and score_delta is not None:
        accepted = passed and (score_delta >= 0.0)
    else:
        accepted = passed

    return CycleEvaluationResult(
        cycle_index=cycle_index,
        skill_name=spec.skill_name,
        harness_spec=spec,
        candidate_report=candidate_report,
        baseline_report=baseline_report,
        accepted=accepted,
        score_delta=score_delta,
        metadata={
            "passing_threshold": passing_threshold,
            "require_non_negative_delta": require_non_negative_delta,
        },
    )


def run_autoharness_loop(
    skill_dict: Union[Dict[str, Any], str, Path],
    candidate_generator: Callable[
        [int, Optional[HarnessDiagnosticReport]],
        Callable[[Dict[str, Any]], Dict[str, Any]],
    ],
    baseline_runner: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
    *,
    max_cycles: int = 3,
    llm_synthesizer: Optional[Callable[[str], str]] = None,
    passing_threshold: float = 0.8,
) -> List[CycleEvaluationResult]:
    """Execute a self-improvement loop across multiple cycles, auto-synthesizing fresh criteria each cycle.

    Terminates early once an improvement candidate is accepted.
    """
    results: List[CycleEvaluationResult] = []
    current_baseline = baseline_runner
    last_report: Optional[HarnessDiagnosticReport] = None

    for cycle in range(1, max_cycles + 1):
        candidate_runner = candidate_generator(cycle, last_report)
        cycle_res = evaluate_improvement_cycle(
            skill_dict=skill_dict,
            candidate_runner=candidate_runner,
            baseline_runner=current_baseline,
            cycle_index=cycle,
            llm_synthesizer=llm_synthesizer,
            passing_threshold=passing_threshold,
        )
        results.append(cycle_res)
        last_report = cycle_res.candidate_report
        if cycle_res.accepted:
            break
        # If candidate improved over baseline, it becomes the new baseline for next cycle
        if cycle_res.score_delta is not None and cycle_res.score_delta > 0:
            current_baseline = candidate_runner

    return results
