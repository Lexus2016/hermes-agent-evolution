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
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Union

logger = logging.getLogger(__name__)

__all__ = [
    "HARNESS_SPEC_JSON_SCHEMA",
    "HARNESS_SYNTHESIS_SYSTEM_PROMPT",
    "HarnessAssertion",
    "TestCaseSpec",
    "HarnessSpec",
    "build_harness_synthesis_prompt",
    "parse_skill_capabilities",
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
    skill_def: Union[Dict[str, Any], str],
    default_category: Optional[str] = None,
) -> HarnessSpec:
    """Synthesize an executable HarnessSpec from a skill's declared capabilities (Issue #2250, Slice A #2516)."""
    parsed = parse_skill_capabilities(skill_def)
    category = (
        default_category if default_category else parsed.get("category", "general")
    )
    capabilities = parsed.get("capabilities", [])

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
        passing_threshold=0.8,
        metadata={
            "description": parsed.get("description", ""),
            "synthesized_by": "AUTOHARNESS",
            "capabilities_count": len(capabilities),
            "test_cases_count": len(test_cases),
        },
    )
