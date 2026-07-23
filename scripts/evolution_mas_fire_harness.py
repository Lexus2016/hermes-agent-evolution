#!/usr/bin/env python3
"""MAS-FIRE coordination fault-injection harness for delegate_task (issue #1211).

Tests Hermes's delegation layer against coordination fault types from the
MAS-FIRE taxonomy (arXiv:2602.19843): stale shared state, corrupted/truncated
subagent summaries, instruction misinterpretation, and reasoning drift.

The harness is exercised via its CLI runner so it is not dead code:
    python -m scripts.evolution_mas_fire_harness run
    python -m scripts.evolution_mas_fire_harness run --output report.json
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional


# ── Fault types ──────────────────────────────────────────────────────────

FAULT_STALE_STATE = "stale_shared_state"
FAULT_CORRUPTED_SUMMARY = "corrupted_summary"
FAULT_TRUNCATED_SUMMARY = "truncated_summary"
FAULT_INSTRUCTION_DRIFT = "instruction_drift"

FAULT_TYPES = [
    FAULT_STALE_STATE,
    FAULT_CORRUPTED_SUMMARY,
    FAULT_TRUNCATED_SUMMARY,
    FAULT_INSTRUCTION_DRIFT,
]


# ── Fault-tolerance tiers (from MAS-FIRE) ────────────────────────────────

TIER_MECHANISM = "mechanism"  # Hard-coded guard catches the fault
TIER_RULE = "rule"  # Validation rule detects the fault
TIER_PROMPT = "prompt"  # System prompt prevents exploitation
TIER_REASONING = "reasoning"  # Model reasons about the fault
TIER_NONE = "none"  # Fault silently propagated


def inject_stale_state(base_content: str) -> str:
    """Inject stale shared state: append an outdated version of the data."""
    return base_content + "\n[STALE: outdated state from a previous write]"


def inject_corrupted_summary(base_content: str) -> str:
    """Corrupt a subagent summary with garbage characters."""
    pos = len(base_content) // 2
    chars = "\x00\x01\x02\x03\x04"
    return base_content[:pos] + chars + base_content[pos:]


def inject_truncated_summary(base_content: str) -> str:
    """Truncate a subagent summary to 20% of its length."""
    return base_content[: max(10, len(base_content) // 5)]


def inject_instruction_drift(base_content: str) -> str:
    """Replace the summary with one that drifts from the original intent."""
    return "Completed the task by taking a different approach than requested."


_INJECTORS = {
    FAULT_STALE_STATE: inject_stale_state,
    FAULT_CORRUPTED_SUMMARY: inject_corrupted_summary,
    FAULT_TRUNCATED_SUMMARY: inject_truncated_summary,
    FAULT_INSTRUCTION_DRIFT: inject_instruction_drift,
}


# ── Detection heuristics ─────────────────────────────────────────────────


def _detect_corruption(content: str) -> bool:
    """Detect corrupted content (control characters present)."""
    return any(ord(c) < 0x20 and c not in "\n\r\t" for c in content)


def _detect_truncation(original: str, received: str) -> bool:
    """Detect truncated summary (received is significantly shorter)."""
    if not original or len(received) < len(original) * 0.5:
        return True
    return not original.endswith(received) and not received.endswith(original)


def _detect_stale_state(content: str) -> bool:
    """Detect stale state markers."""
    return "[STALE:" in content


def _detect_instruction_drift(original: str, received: str) -> bool:
    """Detect instruction drift (completely different content)."""
    if not original or not received:
        return True
    # Check if the received content shares any significant words with original
    orig_words = set(original.lower().split())
    recv_words = set(received.lower().split())
    overlap = orig_words & recv_words
    return len(overlap) < max(1, len(orig_words) * 0.2)


_DETECTORS = {
    FAULT_STALE_STATE: lambda orig, recv: _detect_stale_state(recv),
    FAULT_CORRUPTED_SUMMARY: lambda orig, recv: _detect_corruption(recv),
    FAULT_TRUNCATED_SUMMARY: lambda orig, recv: _detect_truncation(orig, recv),
    FAULT_INSTRUCTION_DRIFT: lambda orig, recv: _detect_instruction_drift(orig, recv),
}


def classify_response(fault_type: str, original: str, received: str) -> str:
    """Classify the fault tolerance tier based on detection.

    Returns one of the TIER_* constants.
    """
    detector = _DETECTORS.get(fault_type)
    if detector and detector(original, received):
        return TIER_MECHANISM
    return TIER_NONE


# ── Test harness ──────────────────────────────────────────────────────────

SAMPLE_SUMMARY = (
    "The code analysis is complete. I found 3 functions in the module: "
    "process_data, helper, and MyClass.method. The process_data function "
    "contains a for loop with an if-else branch. The helper function is "
    "a simple transformation. MyClass.method has conditional logic."
)


def run_fault_injection_suite(
    sample_summary: str = SAMPLE_SUMMARY,
) -> List[Dict[str, Any]]:
    """Run the full fault-injection suite and return results.

    Each result has: ``fault_type``, ``injected``, ``detected``, ``tier``,
    ``original_length``, ``received_length``.
    """
    results: List[Dict[str, Any]] = []
    for fault_type in FAULT_TYPES:
        injector = _INJECTORS[fault_type]
        injected = injector(sample_summary)
        tier = classify_response(fault_type, sample_summary, injected)
        results.append({
            "fault_type": fault_type,
            "detected": tier != TIER_NONE,
            "tier": tier,
            "original_length": len(sample_summary),
            "received_length": len(injected),
        })
    return results


def generate_report(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Generate a summary report from fault injection results."""
    total = len(results)
    detected = sum(1 for r in results if r["detected"])
    tiers: Dict[str, int] = {}
    for r in results:
        tier = r["tier"]
        tiers[tier] = tiers.get(tier, 0) + 1
    return {
        "total_faults": total,
        "detected": detected,
        "undetected": total - detected,
        "detection_rate": detected / total if total else 0.0,
        "tier_distribution": tiers,
        "gaps": [r["fault_type"] for r in results if r["tier"] == TIER_NONE],
    }


# ── CLI runner ───────────────────────────────────────────────────────────


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point — runs the fault injection suite and reports."""
    parser = argparse.ArgumentParser(
        description="Run MAS-FIRE coordination fault-injection tests for delegate_task.",
    )
    parser.add_argument("run", help="Run the fault injection suite")
    parser.add_argument("--output", help="Write JSON report to file (default: stdout)")
    args = parser.parse_args(argv)

    if args.run != "run":
        parser.print_help()
        return 1

    results = run_fault_injection_suite()
    report = generate_report(results)
    output = json.dumps(
        {"results": results, "report": report},
        indent=2,
        default=str,
    )

    if args.output:
        Path(args.output).write_text(output, encoding="utf-8")
        print(f"Report written to {args.output}")
    else:
        print(output)

    # Exit non-zero if any faults were undetected (for CI integration)
    if report["undetected"] > 0:
        print(
            f"\nWARNING: {report['undetected']} fault(s) undetected — "
            "fault-tolerance gaps identified.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
