"""Delegation-scoped reliability primitives: identity adequacy and evidence adequacy.

Implements #3282: Replaces message-level retry/breaker heuristics with
delegation-scoped identity and evidence adequacy (Agent Mesh inspired).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set


class ReliabilityAction(str, Enum):
    PROCEED = "proceed"
    RETRY = "retry"
    ABORT_NON_IDEMPOTENT = "abort_non_idempotent"
    STALLED_NO_PROGRESS = "stalled_no_progress"
    SCHEMA_CORRECTION = "schema_correction"


@dataclass(frozen=True)
class DelegationIdentity:
    """Distinct identity discriminating a delegated unit of work."""

    task_id: str
    parent_session_id: str
    goal_hash: str
    target_role: str = "leaf"
    depth: int = 1
    iteration: int = 1

    @classmethod
    def create(
        cls,
        goal: str,
        parent_session_id: str,
        task_id: Optional[str] = None,
        target_role: str = "leaf",
        depth: int = 1,
        iteration: int = 1,
    ) -> DelegationIdentity:
        g_hash = hashlib.sha256(goal.strip().encode("utf-8")).hexdigest()[:12]
        t_id = task_id or f"del-{g_hash}-{iteration}"
        return cls(
            task_id=t_id,
            parent_session_id=parent_session_id,
            goal_hash=g_hash,
            target_role=target_role,
            depth=depth,
            iteration=iteration,
        )

    @property
    def key(self) -> str:
        return f"{self.parent_session_id}:{self.task_id}:{self.depth}:{self.iteration}"


@dataclass
class DelegationEvidence:
    """Moving, attributable, and deterministic evidence for subagent outcomes."""

    tools_executed: list[str] = field(default_factory=list)
    state_mutations: set[str] = field(default_factory=set)
    has_meaningful_output: bool = False
    schema_valid: bool = True
    is_idempotent: bool = True
    error_count: int = 0
    token_delta: int = 0

    @property
    def has_moving_progress(self) -> bool:
        """Verify that the execution produced moving progress rather than repeating a fixed no-op."""
        return len(self.tools_executed) > 0 or len(self.state_mutations) > 0 or self.token_delta > 50


@dataclass(frozen=True)
class ReliabilityDecision:
    action: ReliabilityAction
    reason: str
    can_retry: bool


class DelegationReliabilityEvaluator:
    """Evaluates identity and evidence adequacy to guide retry and breaker decisions."""

    def __init__(self, max_retries: int = 2):
        self.max_retries = max_retries
        self._history: dict[str, list[DelegationEvidence]] = {}

    def check_identity_adequacy(self, identity: DelegationIdentity) -> bool:
        """Verify that the identity uniquely identifies the work unit."""
        return bool(identity.task_id and identity.parent_session_id and identity.goal_hash)

    def evaluate_outcome(
        self,
        identity: DelegationIdentity,
        evidence: DelegationEvidence,
        success: bool,
    ) -> ReliabilityDecision:
        """Make evidence-adequate reliability decisions."""
        if not self.check_identity_adequacy(identity):
            return ReliabilityDecision(
                action=ReliabilityAction.ABORT_NON_IDEMPOTENT,
                reason="Identity adequacy check failed: missing required identity fields",
                can_retry=False,
            )

        key = identity.key
        if key not in self._history:
            self._history[key] = []
        self._history[key].append(evidence)

        attempts = len(self._history[key])

        # 1. Success case
        if success and evidence.schema_valid and evidence.has_meaningful_output:
            return ReliabilityDecision(
                action=ReliabilityAction.PROCEED,
                reason="Task completed successfully with valid evidence and schema",
                can_retry=False,
            )

        # 2. Schema invalid
        if not evidence.schema_valid:
            if attempts <= self.max_retries:
                return ReliabilityDecision(
                    action=ReliabilityAction.SCHEMA_CORRECTION,
                    reason=f"Output failed schema validation (attempt {attempts}/{self.max_retries})",
                    can_retry=True,
                )
            return ReliabilityDecision(
                action=ReliabilityAction.ABORT_NON_IDEMPOTENT,
                reason="Max schema correction retries exhausted",
                can_retry=False,
            )

        # 3. Non-idempotent task failure
        if not evidence.is_idempotent and len(evidence.state_mutations) > 0:
            return ReliabilityDecision(
                action=ReliabilityAction.ABORT_NON_IDEMPOTENT,
                reason="Task is non-idempotent and has already executed state mutations; retry unsafe",
                can_retry=False,
            )

        # 4. Stalled progress (no moving evidence)
        if not evidence.has_moving_progress:
            return ReliabilityDecision(
                action=ReliabilityAction.STALLED_NO_PROGRESS,
                reason="Subagent failed to produce moving progress signal",
                can_retry=False,
            )

        # 5. Generic retry with budget check
        if attempts <= self.max_retries:
            return ReliabilityDecision(
                action=ReliabilityAction.RETRY,
                reason=f"Retrying failed idempotent task with moving progress (attempt {attempts}/{self.max_retries})",
                can_retry=True,
            )

        return ReliabilityDecision(
            action=ReliabilityAction.ABORT_NON_IDEMPOTENT,
            reason=f"Max retries ({self.max_retries}) exhausted",
            can_retry=False,
        )
