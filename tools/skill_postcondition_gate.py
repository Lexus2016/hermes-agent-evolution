#!/usr/bin/env python3
"""Postcondition validation gate for captured skills (issue #2255, Slice A).

When a skill is captured from a task execution trace, this module validates
that the skill's claimed postcondition actually holds. This replaces
correlation ("skill was present during success") with causation ("skill's
postcondition was independently verified").

This is a **standalone module** — no changes to existing skill loading. The
capture pipeline calls ``validate_capture()`` before persisting a skill; if
it returns ``False``, the skill is not captured.

Trust lifecycle:
  - Skills with a verified postcondition → ``trusted``
  - Skills with a postcondition that failed validation → rejected (not captured)
  - Skills without a verifiable postcondition → ``provisional`` (captured but
    flagged for later validation)

The postcondition is a declarative claim about the expected state after the
skill's procedure executes (e.g. "file exists at /path", "function returns
non-empty dict", "git branch is clean"). Validation runs the check function
against the actual post-execution state.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class Postcondition:
    """A declarative postcondition claim for a captured skill.

    Attributes:
        description: human-readable statement of what should be true.
        check: a callable that receives the post-execution state dict and
            returns True if the postcondition holds, False otherwise.
        required: if True (default), failing the check rejects capture.
            If False, failure downgrades to ``provisional`` instead.
    """

    description: str
    check: Callable[[Dict[str, Any]], bool]
    required: bool = True


@dataclass
class CaptureRecord:
    """Metadata for a skill being captured from a task trace.

    Attributes:
        skill_name: the skill's canonical name.
        procedure_executed: True if the trace shows the skill's specific
            procedure was actually executed (not just loaded).
        postconditions: list of Postcondition objects to validate.
        post_execution_state: the observed state after the task completed,
            passed to each postcondition's check function.
        trust_level: set by validate_capture — ``trusted``, ``provisional``,
            or ``rejected``.
        validation_results: per-postcondition results, set by validate_capture.
    """

    skill_name: str
    procedure_executed: bool
    postconditions: List[Postcondition] = field(default_factory=list)
    post_execution_state: Dict[str, Any] = field(default_factory=dict)
    trust_level: str = "provisional"
    validation_results: List[Dict[str, Any]] = field(default_factory=list)

    def to_metadata(self) -> Dict[str, Any]:
        """Serialize to a metadata dict suitable for skill capture sidecar."""
        return {
            "skill_name": self.skill_name,
            "procedure_executed": self.procedure_executed,
            "postconditions": [
                {"description": pc.description, "required": pc.required}
                for pc in self.postconditions
            ],
            "trust_level": self.trust_level,
            "validation_results": self.validation_results,
        }


def validate_capture(record: CaptureRecord) -> bool:
    """Validate a skill capture record against its postconditions.

    Returns True if the skill should be captured (trusted or provisional),
    False if it should be rejected.

    Rejection criteria:
      1. The skill's procedure was NOT executed in the trace.
      2. A required postcondition check failed.

    Downgrade to provisional:
      - An optional (required=False) postcondition check failed.

    Upgrade to trusted:
      - All required postconditions pass AND procedure was executed.

    Args:
        record: the CaptureRecord to validate (mutated in place — sets
            trust_level and validation_results).

    Returns:
        True if capture should proceed, False if it should be rejected.
    """
    # Gate 1: procedure must have been executed.
    if not record.procedure_executed:
        record.trust_level = "rejected"
        record.validation_results = [
            {
                "description": "procedure execution",
                "required": True,
                "passed": False,
                "reason": "skill procedure was not executed in the trace",
            }
        ]
        logger.info(
            "postcondition_gate: rejected %s — procedure not executed",
            record.skill_name,
        )
        return False

    # Gate 2: a skill must have at least one verifiable postcondition.
    if not record.postconditions:
        record.trust_level = "rejected"
        record.validation_results = [
            {
                "description": "postcondition presence",
                "required": True,
                "passed": False,
                "reason": "no verifiable postcondition declared",
            }
        ]
        logger.info(
            "postcondition_gate: rejected %s — no verifiable postcondition",
            record.skill_name,
        )
        return False

    # Gate 3: validate each postcondition.
    all_required_passed = True
    any_optional_failed = False

    for pc in record.postconditions:
        try:
            passed = bool(pc.check(record.post_execution_state))
        except Exception as exc:
            logger.warning(
                "postcondition_gate: check raised for %s: %s",
                record.skill_name,
                exc,
            )
            passed = False

        result = {
            "description": pc.description,
            "required": pc.required,
            "passed": passed,
        }
        if not passed:
            result["reason"] = "postcondition check returned False"
        record.validation_results.append(result)

        if not passed and pc.required:
            all_required_passed = False
        elif not passed and not pc.required:
            any_optional_failed = True

    # Determine trust level.
    if not all_required_passed:
        record.trust_level = "rejected"
        logger.info(
            "postcondition_gate: rejected %s — required postcondition failed",
            record.skill_name,
        )
        return False
    elif any_optional_failed:
        record.trust_level = "provisional"
        logger.info(
            "postcondition_gate: provisional %s — optional postcondition failed",
            record.skill_name,
        )
        return True
    else:
        record.trust_level = "trusted"
        logger.info(
            "postcondition_gate: trusted %s — all postconditions passed",
            record.skill_name,
        )
        return True


def should_capture(record: CaptureRecord) -> bool:
    """Convenience wrapper: validate and return the capture decision."""
    return validate_capture(record)