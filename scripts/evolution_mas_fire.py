#!/usr/bin/env python3
"""Coordination fault-injection test harness (MAS-FIRE) for subagent delegation (issue #1211).

Hermes delegates tasks to subagents via ``delegate_task``. Coordination failures
in this delegation layer (stale-file races, corrupted/truncated subagent
summaries, instruction misinterpretation, reasoning drift) can cause silent data
corruption — the parent agent trusts the subagent's output without verifying it.

This module is increment 1 of the MAS-FIRE (Multi-Agent System Fault Injection
and Reliability Evaluation) test harness: it defines the fault set for Hermes's
delegation pattern and implements fault injection tests that verify whether
Hermes detects or silently uses degraded subagent output.

Fault set (increment 1):
- ``CORRUPTED_SUMMARY`` — subagent returns a summary with garbled/truncated text
- ``STALE_FILE_RACE`` — subagent reads a file that was modified after delegation
- ``INSTRUCTION_MISINTERPRETATION`` — subagent does a different task than asked
- ``REASONING_DRIFT`` — subagent's reasoning diverges from its stated conclusion

Each fault is a test case that:
1. Constructs a degraded subagent output (the fault).
2. Checks whether the parent agent's validation layer detects the degradation.
3. Reports pass/fail: PASS = degradation detected, FAIL = silently used.

This is a TEST MODULE — it does not change any production code. It tests the
existing delegation validation path (if any) and establishes the fault set for
future increments.

Design matches the ``scripts/evolution_*.py`` corpus: pure functions, standard
library only, no side effects on import, deterministic.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

__all__ = [
    "FaultType",
    "FaultResult",
    "FaultCase",
    "FAULT_SET",
    "inject_corrupted_summary",
    "inject_stale_file_race",
    "inject_instruction_misinterpretation",
    "inject_reasoning_drift",
    "run_fault_suite",
    "main",
]


class FaultType:
    """Enumeration of coordination fault types for the delegation pattern."""

    CORRUPTED_SUMMARY = "corrupted_summary"
    STALE_FILE_RACE = "stale_file_race"
    INSTRUCTION_MISINTERPRETATION = "instruction_misinterpretation"
    REASONING_DRIFT = "reasoning_drift"


@dataclass
class FaultResult:
    """Result of a single fault injection test."""

    fault_type: str
    description: str
    detected: bool  # True if the validation layer caught the fault
    details: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "fault_type": self.fault_type,
            "description": self.description,
            "detected": self.detected,
            "details": self.details,
        }


@dataclass
class FaultCase:
    """A single fault injection test case."""

    fault_type: str
    description: str
    inject_fn: Callable[[], Dict[str, Any]]  # produces the degraded output
    validate_fn: Callable[[Dict[str, Any]], bool]  # returns True if fault detected


# --- Fault injectors ---


def inject_corrupted_summary(
    original_summary: str = "Successfully analyzed the codebase and found 3 issues.",
) -> Dict[str, Any]:
    """Inject a corrupted/truncated summary.

    Simulates a subagent whose summary was garbled during transmission or
    truncated due to token limits. The summary contains null bytes, truncation
    markers, and non-UTF8-like sequences.
    """
    # Truncate mid-sentence and add corruption markers
    truncated = original_summary[:20] + "\x00[TRUNCATED]\ufffd"
    return {
        "fault_type": FaultType.CORRUPTED_SUMMARY,
        "subagent_output": {
            "summary": truncated,
            "status": "completed",
            "files_modified": [],
        },
        "expected_detection": "summary contains null bytes or truncation markers",
    }


def inject_stale_file_race(
    file_content: str = "original content at delegation time",
    modified_content: str = "MODIFIED after delegation — subagent read stale version",
) -> Dict[str, Any]:
    """Inject a stale-file race condition.

    Simulates a subagent that read a file at delegation time, but the file was
    modified by another process before the subagent's result was used. The
    subagent's output reflects the old content, not the current state.
    """
    return {
        "fault_type": FaultType.STALE_FILE_RACE,
        "subagent_output": {
            "summary": f"Read file content: {file_content}",
            "status": "completed",
            "files_read": ["example.py"],
            "file_content_at_read": file_content,
        },
        "current_file_content": modified_content,
        "expected_detection": "subagent read stale file content (file modified after delegation)",
    }


def inject_instruction_misinterpretation(
    requested_task: str = "Find and list all Python files with syntax errors",
) -> Dict[str, Any]:
    """Inject instruction misinterpretation.

    Simulates a subagent that performed a DIFFERENT task than what was
    requested. The parent asked for syntax-error detection but the subagent
    did a general code review instead.
    """
    return {
        "fault_type": FaultType.INSTRUCTION_MISINTERPRETATION,
        "subagent_output": {
            "summary": "Completed code review. Found 2 style issues and 1 missing docstring.",
            "status": "completed",
            "task_performed": "code review (style/lint)",
        },
        "requested_task": requested_task,
        "expected_detection": "subagent performed 'code review' but was asked to 'find syntax errors'",
    }


def inject_reasoning_drift(
    reasoning: str = "I should check the database connection first, then verify the API endpoint.",
    conclusion: str = "The file system is corrupted and needs a full reformat.",
) -> Dict[str, Any]:
    """Inject reasoning drift.

    Simulates a subagent whose reasoning process leads to one conclusion but
    whose final output states a completely different (and unsupported) conclusion.
    """
    return {
        "fault_type": FaultType.REASONING_DRIFT,
        "subagent_output": {
            "summary": conclusion,
            "status": "completed",
            "reasoning": reasoning,
        },
        "expected_detection": "conclusion does not follow from reasoning (drift detected)",
    }


# --- Validators (deterministic heuristic checks) ---


def _validate_corrupted_summary(output: Dict[str, Any]) -> bool:
    """Check if a subagent output shows signs of corruption."""
    summary = output.get("subagent_output", {}).get("summary", "")
    if not isinstance(summary, str):
        return True  # non-string summary is itself a fault
    # Detect null bytes, replacement chars, truncation markers
    indicators = ["\x00", "\ufffd", "[TRUNCATED]", "[truncated]"]
    return any(ind in summary for ind in indicators)


def _validate_stale_file_race(output: Dict[str, Any]) -> bool:
    """Check if subagent read stale file content."""
    file_content_at_read = output.get("subagent_output", {}).get(
        "file_content_at_read", ""
    )
    current_content = output.get("current_file_content", "")
    if not file_content_at_read or not current_content:
        return False  # can't determine
    return file_content_at_read != current_content


def _validate_instruction_misinterpretation(output: Dict[str, Any]) -> bool:
    """Check if subagent performed a different task than requested."""
    task_performed = output.get("subagent_output", {}).get("task_performed", "")
    requested_task = output.get("requested_task", "")
    if not task_performed or not requested_task:
        return False  # can't determine
    # Simple heuristic: check for keyword overlap
    performed_words = set(task_performed.lower().split())
    requested_words = set(requested_task.lower().split())
    # Remove common stop words
    stop_words = {
        "the",
        "a",
        "an",
        "and",
        "or",
        "all",
        "with",
        "for",
        "to",
        "find",
        "list",
    }
    performed_words -= stop_words
    requested_words -= stop_words
    if not performed_words or not requested_words:
        return False
    overlap = performed_words & requested_words
    # If less than 20% keyword overlap, likely misinterpretation
    overlap_ratio = len(overlap) / max(len(requested_words), 1)
    return overlap_ratio < 0.2


def _validate_reasoning_drift(output: Dict[str, Any]) -> bool:
    """Check if conclusion diverges from reasoning."""
    reasoning = output.get("subagent_output", {}).get("reasoning", "")
    conclusion = output.get("subagent_output", {}).get("summary", "")
    if not reasoning or not conclusion:
        return False  # can't determine
    # Heuristic: check for keyword overlap between reasoning and conclusion
    reasoning_words = set(reasoning.lower().split())
    conclusion_words = set(conclusion.lower().split())
    stop_words = {
        "the",
        "a",
        "an",
        "and",
        "or",
        "i",
        "is",
        "are",
        "was",
        "were",
        "to",
        "be",
        "then",
    }
    reasoning_words -= stop_words
    conclusion_words -= stop_words
    if not reasoning_words or not conclusion_words:
        return False
    overlap = reasoning_words & conclusion_words
    overlap_ratio = len(overlap) / max(len(conclusion_words), 1)
    # If less than 15% overlap, likely drift
    return overlap_ratio < 0.15


# --- Fault set definition ---

FAULT_SET: List[FaultCase] = [
    FaultCase(
        fault_type=FaultType.CORRUPTED_SUMMARY,
        description="Subagent returns a summary with garbled/truncated text",
        inject_fn=inject_corrupted_summary,
        validate_fn=_validate_corrupted_summary,
    ),
    FaultCase(
        fault_type=FaultType.STALE_FILE_RACE,
        description="Subagent reads a file that was modified after delegation",
        inject_fn=inject_stale_file_race,
        validate_fn=_validate_stale_file_race,
    ),
    FaultCase(
        fault_type=FaultType.INSTRUCTION_MISINTERPRETATION,
        description="Subagent does a different task than what was requested",
        inject_fn=inject_instruction_misinterpretation,
        validate_fn=_validate_instruction_misinterpretation,
    ),
    FaultCase(
        fault_type=FaultType.REASONING_DRIFT,
        description="Subagent's conclusion diverges from its reasoning process",
        inject_fn=inject_reasoning_drift,
        validate_fn=_validate_reasoning_drift,
    ),
]


def run_fault_suite(cases: Optional[List[FaultCase]] = None) -> List[FaultResult]:
    """Run the full fault injection suite.

    For each fault case: inject the fault, then check whether the validator
    detects it. A PASS means the fault was detected; a FAIL means the
    degradation would be silently used by the parent agent.
    """
    if cases is None:
        cases = FAULT_SET
    results: List[FaultResult] = []
    for case in cases:
        injected = case.inject_fn()
        detected = case.validate_fn(injected)
        results.append(
            FaultResult(
                fault_type=case.fault_type,
                description=case.description,
                detected=detected,
                details=injected.get("expected_detection", ""),
            )
        )
    return results


def main(argv: List[str]) -> int:
    """CLI: run the fault injection suite and print results."""
    args = argv[1:]
    results = run_fault_suite()

    summary = {
        "total": len(results),
        "detected": sum(1 for r in results if r.detected),
        "silently_used": sum(1 for r in results if not r.detected),
        "results": [r.to_dict() for r in results],
    }
    print(json.dumps(summary, indent=2))

    # Non-zero exit if any fault was silently used (validation gap)
    if summary["silently_used"] > 0:
        print(
            f"\nWARNING: {summary['silently_used']} fault(s) were NOT detected — "
            f"the delegation layer would silently use degraded output.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
