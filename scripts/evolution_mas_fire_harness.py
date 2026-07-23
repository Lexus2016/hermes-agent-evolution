#!/usr/bin/env python3
"""MAS-FIRE coordination fault-injection test harness (#1211).

Implements a fault-injection test harness for Hermes's delegation layer
(``delegate_task``), covering the coordination fault types identified by
MAS-FIRE (arXiv:2602.19843):

- **Stale shared state**: two subagents read the same file; one writes, the
  other's work is based on stale data.
- **Corrupted/truncated subagent summary**: a subagent's summary is corrupted
  or truncated, and the parent acts on wrong information.
- **Instruction misinterpretation**: a subagent misinterprets the parent's
  intent, producing output that diverges from the task goal.
- **Reasoning drift**: a subagent drifts from the parent's intent without
  the parent knowing.
- **Lost update**: a subagent's update to shared state is overwritten or lost.

The harness defines the fault set, provides injection mechanisms, and scores
Hermes's fault tolerance into the four tiers MAS-FIRE identifies:
mechanism, rule, prompt, reasoning.

DESIGN — pure test harness, no core coupling.
``FaultInjector`` provides methods to inject each fault type into mock
subagent outputs.  ``FaultToleranceScorer`` classifies the response.  The
harness is designed to be run against mock delegation scenarios — no real
LLM calls or real subagents are needed.

Usage::

    harness = FaultInjector()
    fault = harness.inject_corrupted_summary("Do the task", "DO THE TA")
    scorer = FaultToleranceScorer()
    result = scorer.score(fault, parent_response="I notice the summary is garbled...")
    assert result.tier >= FaultToleranceTier.MECHANISM
"""

from __future__ import annotations

import random
import string
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Callable, Dict, List, Optional, Tuple


class FaultType:
    """Coordination fault types from MAS-FIRE applied to Hermes delegation."""

    STALE_SHARED_STATE = "stale_shared_state"
    CORRUPTED_SUMMARY = "corrupted_summary"
    TRUNCATED_SUMMARY = "truncated_summary"
    INSTRUCTION_MISINTERPRETATION = "instruction_misinterpretation"
    REASONING_DRIFT = "reasoning_drift"
    LOST_UPDATE = "lost_update"

    ALL: Tuple[str, ...] = (
        STALE_SHARED_STATE,
        CORRUPTED_SUMMARY,
        TRUNCATED_SUMMARY,
        INSTRUCTION_MISINTERPRETATION,
        REASONING_DRIFT,
        LOST_UPDATE,
    )


class FaultToleranceTier(IntEnum):
    """MAS-FIRE fault-tolerance tiers (higher = more resilient).

    - NONE: the fault was silently propagated — no detection or recovery.
    - MECHANISM: a mechanism (timeout, checksum, retry) caught the fault.
    - RULE: a validation rule (schema check, content match) caught it.
    - PROMPT: the prompt design (structured output, explicit constraints)
      prevented the fault from causing harm.
    - REASONING: the agent's own reasoning detected and recovered from
      the fault — the highest tier.
    """

    NONE = 0
    MECHANISM = 1
    RULE = 2
    PROMPT = 3
    REASONING = 4

    @property
    def label(self) -> str:
        return {
            0: "none",
            1: "mechanism",
            2: "rule",
            3: "prompt",
            4: "reasoning",
        }[self.value]


@dataclass
class FaultResult:
    """Result of a single fault injection + scoring cycle."""

    fault_type: str
    original_input: str
    faulty_output: str
    parent_response: str
    tier: FaultToleranceTier
    detected: bool
    evidence: List[str] = field(default_factory=list)
    gap_description: str = ""


class FaultInjector:
    """Injects coordination faults into mock subagent outputs.

    Each method takes a clean input/output and returns a corrupted version
    that simulates the named fault type.
    """

    @staticmethod
    def inject_corrupted_summary(
        task_goal: str,
        clean_summary: str,
        corruption_rate: float = 0.1,
    ) -> str:
        """Corrupt a subagent summary by replacing random characters.

        Args:
            task_goal: The original task goal (unused, for context).
            clean_summary: The clean subagent summary to corrupt.
            corruption_rate: Fraction of characters to corrupt (0-1).

        Returns:
            A corrupted version of the summary.
        """
        chars = list(clean_summary)
        n_corrupt = max(1, int(len(chars) * corruption_rate))
        indices = random.sample(range(len(chars)), min(n_corrupt, len(chars)))
        for idx in indices:
            chars[idx] = random.choice(string.printable[:62])
        return "".join(chars)

    @staticmethod
    def inject_truncated_summary(
        clean_summary: str,
        truncation_ratio: float = 0.3,
    ) -> str:
        """Truncate a subagent summary to simulate message loss.

        Args:
            clean_summary: The clean summary to truncate.
            truncation_ratio: Fraction of the summary to keep (0-1).

        Returns:
            A truncated version of the summary.
        """
        keep_len = max(1, int(len(clean_summary) * truncation_ratio))
        return clean_summary[:keep_len] + "...[truncated]"

    @staticmethod
    def inject_stale_shared_state(
        file_content_v1: str,
        file_content_v2: str,
    ) -> Dict[str, Any]:
        """Simulate a stale-file race between two subagents.

        Args:
            file_content_v1: The file content as read by subagent A (stale).
            file_content_v2: The file content as written by subagent B (current).

        Returns:
            A dict with the stale read and the current write, simulating
            the race condition.
        """
        return {
            "subagent_a_stale_read": file_content_v1,
            "subagent_b_current_write": file_content_v2,
            "divergence": file_content_v1 != file_content_v2,
        }

    @staticmethod
    def inject_instruction_misinterpretation(
        original_instruction: str,
        misinterpreted_as: str,
    ) -> Dict[str, str]:
        """Simulate a subagent misinterpreting the parent's instruction.

        Args:
            original_instruction: What the parent actually asked for.
            misinterpreted_as: What the subagent did instead.

        Returns:
            A dict with the original and misinterpreted instructions.
        """
        return {
            "original_instruction": original_instruction,
            "subagent_interpretation": misinterpreted_as,
            "divergence": original_instruction != misinterpreted_as,
        }

    @staticmethod
    def inject_reasoning_drift(
        task_goal: str,
        drifted_output: str,
    ) -> Dict[str, str]:
        """Simulate a subagent drifting from the task goal.

        Args:
            task_goal: The original task goal.
            drifted_output: The output the subagent produced after drifting.

        Returns:
            A dict with the goal and the drifted output.
        """
        return {
            "task_goal": task_goal,
            "subagent_output": drifted_output,
            "drifted": task_goal.lower() not in drifted_output.lower(),
        }

    @staticmethod
    def inject_lost_update(
        update_content: str,
        overwrite_content: str,
    ) -> Dict[str, str]:
        """Simulate a lost update — subagent A's write is overwritten by B.

        Args:
            update_content: What subagent A wrote.
            overwrite_content: What subagent B overwrote it with.

        Returns:
            A dict showing the lost update scenario.
        """
        return {
            "subagent_a_update": update_content,
            "subagent_b_overwrite": overwrite_content,
            "update_lost": update_content != overwrite_content,
        }


class FaultToleranceScorer:
    """Scores Hermes's fault tolerance by classifying responses to faults.

    The scorer examines the parent agent's response to a faulty subagent
    output and classifies it into one of the MAS-FIRE fault-tolerance tiers.
    """

    # Keywords that indicate detection at each tier
    _MECHANISM_SIGNALS = [
        "timeout",
        "checksum",
        "retry",
        "error",
        "failed",
        "exception",
        "malformed",
        "invalid format",
        "parse error",
    ]
    _RULE_SIGNALS = [
        "schema",
        "validation",
        "does not match",
        "expected",
        "mismatch",
        "incomplete",
        "truncated",
        "corrupted",
    ]
    _PROMPT_SIGNALS = [
        "structured",
        "format",
        "template",
        "constraint",
        "required field",
    ]
    _REASONING_SIGNALS = [
        "i notice",
        "appears",
        "seems",
        "inconsistent",
        "does not align",
        "unexpected",
        "anomal",
        "suspicious",
        "discrepancy",
        "contradict",
    ]

    def score(
        self,
        fault_type: str,
        faulty_output: str,
        parent_response: str,
        original_input: Optional[str] = None,
    ) -> FaultResult:
        """Score the parent agent's response to an injected fault.

        Args:
            fault_type: One of ``FaultType.ALL``.
            faulty_output: The corrupted subagent output.
            parent_response: The parent agent's response to the faulty output.
            original_input: The original clean input (for context).

        Returns:
            A ``FaultResult`` with the tier, detection status, and evidence.
        """
        response_lower = parent_response.lower()
        evidence: List[str] = []
        detected = False
        tier = FaultToleranceTier.NONE

        # Check for detection signals at each tier (highest wins)
        for signal in self._REASONING_SIGNALS:
            if signal in response_lower:
                evidence.append(f"Reasoning signal: '{signal}'")
                tier = max(tier, FaultToleranceTier.REASONING)
                detected = True
                break

        if tier < FaultToleranceTier.RULE:
            for signal in self._RULE_SIGNALS:
                if signal in response_lower:
                    evidence.append(f"Rule signal: '{signal}'")
                    tier = max(tier, FaultToleranceTier.RULE)
                    detected = True
                    break

        if tier < FaultToleranceTier.MECHANISM:
            for signal in self._MECHANISM_SIGNALS:
                if signal in response_lower:
                    evidence.append(f"Mechanism signal: '{signal}'")
                    tier = max(tier, FaultToleranceTier.MECHANISM)
                    detected = True
                    break

        # Check for prompt-level prevention (structured output that avoids harm)
        if tier < FaultToleranceTier.PROMPT:
            for signal in self._PROMPT_SIGNALS:
                if signal in response_lower:
                    evidence.append(f"Prompt signal: '{signal}'")
                    tier = max(tier, FaultToleranceTier.PROMPT)
                    detected = True
                    break

        # If not detected, check if the response blindly uses the faulty output
        if not detected:
            if faulty_output[:20].lower() in response_lower:
                evidence.append("Faulty output was used without detection")
                tier = FaultToleranceTier.NONE

        gap = ""
        if not detected:
            gap = f"Fault type '{fault_type}' was silently propagated — no detection mechanism caught it."

        return FaultResult(
            fault_type=fault_type,
            original_input=original_input or "",
            faulty_output=faulty_output,
            parent_response=parent_response,
            tier=tier,
            detected=detected,
            evidence=evidence,
            gap_description=gap,
        )


def run_fault_injection_suite(
    parent_response_fn: Callable[[str, str], str],
) -> List[FaultResult]:
    """Run the full MAS-FIRE fault injection suite.

    Args:
        parent_response_fn: A function that takes (fault_type, faulty_output)
                           and returns the parent agent's response.  In real
                           usage this would call the agent; in tests it's
                           a mock.

    Returns:
        A list of ``FaultResult`` objects, one per fault type.
    """
    injector = FaultInjector()
    scorer = FaultToleranceScorer()
    results: List[FaultResult] = []

    # 1. Corrupted summary
    clean = "The analysis is complete with 3 findings."
    corrupted = injector.inject_corrupted_summary("Analyze the report", clean)
    response = parent_response_fn(FaultType.CORRUPTED_SUMMARY, corrupted)
    results.append(
        scorer.score(FaultType.CORRUPTED_SUMMARY, corrupted, response, clean)
    )

    # 2. Truncated summary
    clean = "Task completed. Found 5 issues in the codebase that need attention."
    truncated = injector.inject_truncated_summary(clean, 0.3)
    response = parent_response_fn(FaultType.TRUNCATED_SUMMARY, truncated)
    results.append(
        scorer.score(FaultType.TRUNCATED_SUMMARY, truncated, response, clean)
    )

    # 3. Stale shared state
    stale = injector.inject_stale_shared_state("version=1", "version=2")
    response = parent_response_fn(FaultType.STALE_SHARED_STATE, str(stale))
    results.append(scorer.score(FaultType.STALE_SHARED_STATE, str(stale), response))

    # 4. Instruction misinterpretation
    misinterp = injector.inject_instruction_misinterpretation(
        "Sort files by size", "Sort files by name"
    )
    response = parent_response_fn(
        FaultType.INSTRUCTION_MISINTERPRETATION, str(misinterp)
    )
    results.append(
        scorer.score(FaultType.INSTRUCTION_MISINTERPRETATION, str(misinterp), response)
    )

    # 5. Reasoning drift
    drift = injector.inject_reasoning_drift(
        "analyze security vulnerabilities", "I wrote a poem about cats"
    )
    response = parent_response_fn(FaultType.REASONING_DRIFT, str(drift))
    results.append(scorer.score(FaultType.REASONING_DRIFT, str(drift), response))

    # 6. Lost update
    lost = injector.inject_lost_update("subagent A: data=v1", "subagent B: data=v2")
    response = parent_response_fn(FaultType.LOST_UPDATE, str(lost))
    results.append(scorer.score(FaultType.LOST_UPDATE, str(lost), response))

    return results


def summarize_results(results: List[FaultResult]) -> Dict[str, Any]:
    """Summarize a list of FaultResults into a report dict.

    Args:
        results: List of FaultResult objects from ``run_fault_injection_suite``.

    Returns:
        A dict with per-fault-type tier, detection rate, and identified gaps.
    """
    total = len(results)
    detected = sum(1 for r in results if r.detected)
    gaps = [r.gap_description for r in results if r.gap_description]
    return {
        "total_faults": total,
        "detected": detected,
        "detection_rate": detected / total if total > 0 else 0.0,
        "per_fault": [
            {
                "fault_type": r.fault_type,
                "tier": r.tier.label,
                "detected": r.detected,
                "evidence": r.evidence,
            }
            for r in results
        ],
        "gaps": gaps,
        "average_tier": sum(r.tier.value for r in results) / total
        if total > 0
        else 0.0,
    }
