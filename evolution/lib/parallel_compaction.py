# -*- coding: utf-8 -*-
"""Asynchronous early-trigger parallel context compaction.

Issue #2469 (parent #2186) — arXiv:2605.23296.

Decouples compaction from the hard context limit by triggering summarization
in the background slightly before the hard limit is reached (using a
configurable headroom). The agent continues executing in parallel with
the summarization worker. When summarization completes, the compacted
context is swapped in safely at turn boundaries without mid-turn truncation.
"""

from __future__ import annotations

import concurrent.futures
from dataclasses import asdict, dataclass, field
from enum import Enum
import re
from typing import Any, Callable, Dict, List, Optional


class CompactionState(Enum):
    """Lifecycle state of an asynchronous compaction pass."""

    IDLE = "idle"
    COMPACTING = "compacting"
    READY_TO_SWAP = "ready_to_swap"
    SWAPPED = "swapped"
    FAILED = "failed"


@dataclass
class SummaryVolumeConstraint:
    """Hard structural volume constraint for context summarization."""

    section_count: int = 6
    per_section_token_cap: int = 250
    enabled: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SummaryVolumeConstraint":
        return cls(**{k: d[k] for k in d if k in cls.__dataclass_fields__})


@dataclass
class SummaryValidationResult:
    """Outcome of post-validating a summary against a volume constraint."""

    ok: bool
    detected_sections: int
    expected_sections: int
    overlong_sections: List[int]
    violations: List[str]


def estimate_tokens(text: str) -> int:
    """Rough token estimate (~4 chars/token); 0 for empty input."""
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


_SECTION_HEADING_RE = re.compile(r"^#{1,6}\s+\S")


def split_sections(summary: str) -> List[str]:
    """Split a summary into sections on markdown headings (``## ...``)."""
    sections: List[str] = []
    current: List[str] = []
    for line in summary.splitlines():
        if _SECTION_HEADING_RE.match(line.strip()):
            if current:
                sections.append("\n".join(current).rstrip())
            current = [line]
        else:
            current.append(line)
    if current:
        sections.append("\n".join(current).rstrip())
    return [s for s in sections if s.strip()]


def validate_summary_volume(
    summary: str,
    constraint: SummaryVolumeConstraint,
) -> SummaryValidationResult:
    """Post-validate a summary against the volume constraint; disabled always ok."""
    if not constraint.enabled:
        return SummaryValidationResult(True, 0, constraint.section_count, [], [])

    sections = split_sections(summary)
    detected = len(sections)
    violations: List[str] = []
    overlong: List[int] = []
    if detected != constraint.section_count:
        violations.append(
            f"expected {constraint.section_count} sections, got {detected}"
        )
    for i, section in enumerate(sections, start=1):
        if estimate_tokens(section) > constraint.per_section_token_cap:
            overlong.append(i)
            violations.append(
                f"section {i} exceeds {constraint.per_section_token_cap} tokens"
            )

    return SummaryValidationResult(
        not violations, detected, constraint.section_count, overlong, violations
    )


@dataclass
class ParallelCompactionConfig:
    """Configuration for parallel context compaction.

    Attributes:
        enabled: Master toggle for early-trigger parallel compaction.
        hard_token_limit: Upper bound context capacity for the model.
        headroom_ratio: Fraction of hard limit to reserve as early-trigger headroom.
        explicit_headroom_tokens: Optional explicit token count for headroom override.
        preserve_recent_count: Number of recent messages to exclude from compression.
        summary_constraint: Optional hard structural constraint on summary volume.
    """

    enabled: bool = True
    hard_token_limit: int = 100000
    headroom_ratio: float = 0.15
    explicit_headroom_tokens: Optional[int] = None
    preserve_recent_count: int = 4
    summary_constraint: Optional[SummaryVolumeConstraint] = None

    @property
    def headroom_tokens(self) -> int:
        if self.explicit_headroom_tokens is not None:
            return max(0, self.explicit_headroom_tokens)
        return max(1, int(self.hard_token_limit * self.headroom_ratio))

    @property
    def early_trigger_threshold(self) -> int:
        return max(0, self.hard_token_limit - self.headroom_tokens)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ParallelCompactionConfig":
        kwargs = {k: d[k] for k in d if k in cls.__dataclass_fields__}
        if isinstance(kwargs.get("summary_constraint"), dict):
            kwargs["summary_constraint"] = SummaryVolumeConstraint.from_dict(
                kwargs["summary_constraint"]
            )
        return cls(**kwargs)


@dataclass
class CompactionSnapshot:
    """Snapshot captured when background compaction was triggered."""

    trigger_index: int
    message_count: int
    messages: List[Dict[str, Any]]
    token_count_at_trigger: int


class ParallelCompactor:
    """Orchestrates early-triggered background context compaction and safe turn swaps."""

    def __init__(
        self,
        config: Optional[ParallelCompactionConfig] = None,
        executor: Optional[concurrent.futures.Executor] = None,
    ) -> None:
        self.config = config or ParallelCompactionConfig()
        self._executor = executor
        self._state = CompactionState.IDLE
        self._future: Optional[concurrent.futures.Future[List[Dict[str, Any]]]] = None
        self._snapshot: Optional[CompactionSnapshot] = None
        self._compacted_prefix: Optional[List[Dict[str, Any]]] = None
        self._last_error: Optional[str] = None
        self._swap_count: int = 0

    @property
    def state(self) -> CompactionState:
        return self._state

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    @property
    def swap_count(self) -> int:
        return self._swap_count

    def should_trigger(self, current_tokens: int) -> bool:
        """Check whether current token usage crossed early trigger threshold."""
        if not self.config.enabled:
            return False
        if self._state not in (CompactionState.IDLE, CompactionState.SWAPPED):
            return False
        return current_tokens >= self.config.early_trigger_threshold

    def start_async_compaction(
        self,
        messages: List[Dict[str, Any]],
        current_tokens: int,
        summarize_fn: Callable[[List[Dict[str, Any]]], List[Dict[str, Any]]],
    ) -> bool:
        """Launch background summarization on a snapshot of messages."""
        if not self.config.enabled:
            return False
        if self._state == CompactionState.COMPACTING:
            return False

        snapshot_messages = [dict(m) for m in messages]
        self._snapshot = CompactionSnapshot(
            trigger_index=len(snapshot_messages),
            message_count=len(snapshot_messages),
            messages=snapshot_messages,
            token_count_at_trigger=current_tokens,
        )
        self._state = CompactionState.COMPACTING
        self._last_error = None
        self._compacted_prefix = None

        if self._executor is not None:
            self._future = self._executor.submit(summarize_fn, snapshot_messages)
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                # Synchronous fallback if no executor provided
                try:
                    res = summarize_fn(snapshot_messages)
                    self._compacted_prefix = res
                    self._state = CompactionState.READY_TO_SWAP
                    return True
                except Exception as exc:
                    self._last_error = str(exc)
                    self._state = CompactionState.FAILED
                    return False

        return True

    def poll_status(self) -> CompactionState:
        """Update and return the compaction status."""
        if self._state == CompactionState.COMPACTING and self._future is not None:
            if self._future.done():
                try:
                    self._compacted_prefix = self._future.result()
                    self._state = CompactionState.READY_TO_SWAP
                except Exception as exc:
                    self._last_error = str(exc)
                    self._state = CompactionState.FAILED
        return self._state

    def is_safe_swap_boundary(self, current_messages: List[Dict[str, Any]]) -> bool:
        """Verify context is at a safe turn boundary before swapping.

        Safe boundary rules:
        - No un-responded tool calls (last message cannot be tool_calls without tool response).
        - Must have at least 1 message.
        """
        if not current_messages:
            return False

        last_msg = current_messages[-1]
        role = last_msg.get("role")

        # Incomplete tool execution turn is not safe
        if role == "assistant" and last_msg.get("tool_calls"):
            return False
        if role == "tool":
            # Mid-tool execution sequence; check if more tool calls are pending
            return False

        return True

    def try_apply_swap(
        self, current_messages: List[Dict[str, Any]]
    ) -> Optional[List[Dict[str, Any]]]:
        """Swap in compacted prefix with new messages appended if ready and safe."""
        self.poll_status()
        if self._state != CompactionState.READY_TO_SWAP:
            return None
        if not self.is_safe_swap_boundary(current_messages):
            return None
        if self._snapshot is None or self._compacted_prefix is None:
            return None

        # Messages added while background compaction was running
        trigger_index = self._snapshot.trigger_index
        delta_messages = current_messages[trigger_index:]

        # Merge compacted prefix with newly arrived delta messages
        new_messages = list(self._compacted_prefix) + [dict(m) for m in delta_messages]

        self._state = CompactionState.SWAPPED
        self._swap_count += 1
        return new_messages

    def build_summary_instruction(self) -> str:
        """Structural summarization instruction; empty when unconfigured."""
        constraint = self.config.summary_constraint
        if constraint is None or not constraint.enabled:
            return ""
        return (
            f"Produce a summary with EXACTLY {constraint.section_count} '##' sections, "
            f"each at most {constraint.per_section_token_cap} tokens; no extra content."
        )

    def validate_summary(self, summary: str) -> SummaryValidationResult:
        """Post-validate a produced summary; unconfigured always validates."""
        constraint = self.config.summary_constraint
        if constraint is None:
            return SummaryValidationResult(True, 0, 0, [], [])
        return validate_summary_volume(summary, constraint)

    def build_reprompt_instruction(self, result: SummaryValidationResult) -> str:
        """Corrective re-prompt for a violating summary; empty when valid."""
        if result.ok:
            return ""
        return (
            "Summary violated structural constraints:\n- "
            + "\n- ".join(result.violations)
            + "\nRewrite the summary to comply exactly."
        )

    def reset(self) -> None:
        """Reset state for next cycle."""
        self._state = CompactionState.IDLE
        self._future = None
        self._snapshot = None
        self._compacted_prefix = None
        self._last_error = None
