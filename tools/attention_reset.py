#!/usr/bin/env python3
"""
Attention Reset Mechanism (SSoT-style)

A lightweight, deterministic attention-reset ritual to stop agents from
fixating in long sessions. The agent emits 10 fresh alphanumeric characters
from its own output, derives a position from the digit values, and announces
the selected character — a forced pause + small generative act + content-derived
selection that breaks the inertia of the first interpretation.

Mechanism:
1. The agent emits 10 fresh alphanumeric characters from its own output
   (no RNG tool, no reuse)
2. position = (sum of all digit values in the string) mod 10 (0-indexed)
3. Pick string[position]
4. Announce: reset: <string> (digit-sum S, pos N): <char>
5. Re-engage the task with fresh attention

When to trigger:
- After >= 2 failed attempts at the same problem
- Before a hard-to-reverse decision (schema/migration, merge, release, deploy)
- When the agent notices it "already knows the answer" without having checked
- When it is continuing prior reasoning instead of looking fresh

What it is NOT:
- Not a tie-breaker between options
- Not a post-hoc justification engine
- Not a scheduled no-op ritual

The value is the pause + small deliberate act, which interrupts the first
interpretation and forces reconsideration.
"""

from __future__ import annotations

import logging
import random
import string
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class ResetContext:
    """Context for an attention reset."""

    trigger_reason: str
    failed_attempts: int = 0
    task_description: str = ""
    current_hypothesis: str = ""
    reset_count: int = 0


@dataclass
class ResetResult:
    """Result of an attention reset."""

    generated_string: str
    digit_sum: int
    position: int
    selected_char: str
    announcement: str
    context: ResetContext
    timestamp: float = field(default_factory=time.time)

    def __str__(self) -> str:
        return self.announcement


class AttentionReset:
    """
    Attention reset mechanism to prevent fixation and tunnel vision.

    Tracks failed attempts and triggers resets when:
    - >= 2 failed attempts at the same problem
    - Before hard-to-reverse decisions
    - Agent appears overconfident without verification
    - Continuing prior reasoning without fresh perspective
    """

    # Reset trigger thresholds
    FAILED_ATTEMPT_THRESHOLD = 2
    MAX_RESETS_PER_TASK = 5

    # Random seed based on recent output (not RNG tool)
    _random_seed: Optional[int] = None
    _last_output_hash: Optional[int] = None

    def __init__(
        self,
        failed_attempt_threshold: int = FAILED_ATTEMPT_THRESHOLD,
        max_resets_per_task: int = MAX_RESETS_PER_TASK,
    ):
        """Initialize attention reset mechanism.

        Args:
            failed_attempt_threshold: Trigger reset after this many failed attempts
            max_resets_per_task: Maximum resets allowed before aborting task
        """
        self.failed_attempt_threshold = failed_attempt_threshold
        self.max_resets_per_task = max_resets_per_task
        self._reset_count = 0
        self._failed_attempts = 0
        self._last_trigger_reason = None

    def reset_state(self) -> None:
        """Reset the attempt counter (e.g., when starting a new task)."""
        self._failed_attempts = 0
        self._reset_count = 0
        self._last_trigger_reason = None

    def record_failure(
        self,
        reason: str = "generic_failure",
        hypothesis: str = "",
        task_description: str = "",
    ) -> None:
        """Record a failed attempt at solving a problem.

        Args:
            reason: Description of why the attempt failed
            hypothesis: The hypothesis that was being tested
            task_description: Description of the overall task
        """
        self._failed_attempts += 1
        self._last_trigger_reason = reason
        logger.debug(
            f"Failed attempt #{self._failed_attempts}: {reason}. "
            f"Hypothesis: {hypothesis[:100] if hypothesis else 'none'}..."
        )

    def record_success(self) -> None:
        """Record a successful attempt (resets failure counter)."""
        self._failed_attempts = 0
        self._last_trigger_reason = None
        logger.debug("Success recorded, reset failure counter")

    def should_trigger_reset(
        self,
        before_irreversible: bool = False,
        overconfident_without_check: bool = False,
        continuing_prior_reasoning: bool = False,
    ) -> bool:
        """Check if an attention reset should be triggered.

        Args:
            before_irreversible: This is before a hard-to-reverse decision
            overconfident_without_check: Agent is confident but hasn't verified
            continuing_prior_reasoning: Agent is continuing old reasoning

        Returns:
            True if reset should be triggered
        """
        # Check reset limit
        if self._reset_count >= self.max_resets_per_task:
            logger.warning(
                f"Max resets ({self.max_resets_per_task}) reached for this task. "
                "Consider aborting and reconsidering the entire approach."
            )
            return False

        # Trigger conditions
        if self._failed_attempts >= self.failed_attempt_threshold:
            logger.info(
                f"Triggering reset after {self._failed_attempts} failed attempts"
            )
            return True

        if before_irreversible:
            logger.info("Triggering reset before irreversible action")
            return True

        if overconfident_without_check:
            logger.info("Triggering reset due to overconfidence without verification")
            return True

        if continuing_prior_reasoning:
            logger.info("Triggering reset due to continuing prior reasoning")
            return True

        return False

    def trigger_reset(
        self,
        seed_from_output: Optional[str] = None,
        context: Optional[ResetContext] = None,
    ) -> ResetResult:
        """
        Execute an attention reset.

        Args:
            seed_from_output: Optional seed string from recent output (for determinism)
            context: Optional context about why reset is being triggered

        Returns:
            ResetResult with the generated string, selection, and announcement
        """
        self._reset_count += 1

        # Generate 10 fresh alphanumeric characters
        # If seed_from_output is provided, use it for deterministic generation
        if seed_from_output:
            # Use hash of output as seed
            seed = hash(seed_from_output + str(self._reset_count)) % (2**32)
            rng = random.Random(seed)
            chars = "".join(rng.choices(string.ascii_letters + string.digits, k=10))
        else:
            # Use system randomness but not predictable
            rng = random.Random()
            chars = "".join(rng.choices(string.ascii_letters + string.digits, k=10))

        # Calculate digit sum
        digit_sum = sum(int(c) for c in chars if c.isdigit())

        # Derive position from digit sum (0-indexed, mod 10)
        position = digit_sum % 10

        # Select character at position
        if position < len(chars):
            selected_char = chars[position]
        else:
            selected_char = chars[-1]

        # Create announcement
        announcement = (
            f"reset: {chars} (digit-sum {digit_sum}, pos {position}): {selected_char}"
        )

        # Create context if not provided
        if context is None:
            context = ResetContext(
                trigger_reason=self._last_trigger_reason or "manual",
                failed_attempts=self._failed_attempts,
                reset_count=self._reset_count,
            )
        else:
            context.reset_count = self._reset_count
            context.failed_attempts = self._failed_attempts

        result = ResetResult(
            generated_string=chars,
            digit_sum=digit_sum,
            position=position,
            selected_char=selected_char,
            announcement=announcement,
            context=context,
        )

        logger.info(f"Attention reset performed: {announcement}")

        # Reset failed attempts after successful reset
        self._failed_attempts = 0

        return result

    def get_reset_prompt(self, reset_result: Optional[ResetResult] = None) -> str:
        """
        Generate a prompt to guide the agent after a reset.

        Args:
            reset_result: Optional reset result to include in prompt

        Returns:
            Prompt string for the agent
        """
        if reset_result:
            base = f"{reset_result.announcement}\n\n"
        else:
            base = "Attention reset triggered.\n\n"

        prompt = f"""{base}**You have just performed an attention reset.** 

Take a moment to breathe and step back from your previous line of reasoning.

Instructions:
1. Re-read the original task description from scratch
2. Consider alternative approaches you may have dismissed
3. Verify your previous assumptions against the actual problem
4. Look for evidence that contradicts your current hypothesis
5. If you still believe your approach is correct, explicitly state why

The purpose of this reset is to break fixation and tunnel vision — 
common failure modes in long debugging sessions. Use this opportunity 
to view the problem from a fresh perspective."""

        return prompt

    def should_abort_task(self) -> bool:
        """Check if task should be aborted due to excessive resets.

        Returns:
            True if too many resets have been performed
        """
        if self._reset_count >= self.max_resets_per_task:
            logger.warning(
                f"Task abort recommended: {self._reset_count} resets performed "
                f"(max: {self.max_resets_per_task})"
            )
            return True
        return False

    def get_state_summary(self) -> dict:
        """Get current state summary for debugging/logging.

        Returns:
            Dictionary with current state
        """
        return {
            "failed_attempts": self._failed_attempts,
            "reset_count": self._reset_count,
            "last_trigger_reason": self._last_trigger_reason,
            "threshold": self.failed_attempt_threshold,
            "max_resets": self.max_resets_per_task,
        }


def attention_reset_hook(
    failed_attempts: int,
    hypothesis: str = "",
    task_description: str = "",
    before_irreversible: bool = False,
) -> Optional[str]:
    """
    Convenience hook function for triggering attention resets.

    Args:
        failed_attempts: Number of failed attempts so far
        hypothesis: Current hypothesis being tested
        task_description: Description of the task
        before_irreversible: Whether this is before an irreversible action

    Returns:
        Reset announcement if triggered, None otherwise
    """
    reset = AttentionReset()
    reset._failed_attempts = failed_attempts

    if reset.should_trigger_reset(before_irreversible=before_irreversible):
        context = ResetContext(
            trigger_reason="failed_attempts"
            if failed_attempts >= 2
            else "irreversible",
            failed_attempts=failed_attempts,
            task_description=task_description,
            current_hypothesis=hypothesis,
        )
        result = reset.trigger_reset(context=context)
        return result.announcement

    return None
