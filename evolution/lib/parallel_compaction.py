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
import json
from pathlib import Path
import re
import time
from typing import Any, Callable, Dict, List, Optional, Union
import uuid


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
        save_snapshot: Whether to persist pre-compaction snapshot to disk.
        snapshot_dir: Optional explicit directory for snapshot storage.
    """

    enabled: bool = True
    hard_token_limit: int = 100000
    headroom_ratio: float = 0.15
    explicit_headroom_tokens: Optional[int] = None
    preserve_recent_count: int = 4
    summary_constraint: Optional[SummaryVolumeConstraint] = None
    save_snapshot: bool = True
    snapshot_dir: Optional[str] = None

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
    snapshot_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "timestamp": self.timestamp,
            "trigger_index": self.trigger_index,
            "message_count": self.message_count,
            "token_count_at_trigger": self.token_count_at_trigger,
            "messages": self.messages,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "CompactionSnapshot":
        return cls(
            trigger_index=d.get("trigger_index", len(d.get("messages", []))),
            message_count=d.get("message_count", len(d.get("messages", []))),
            messages=d.get("messages", []),
            token_count_at_trigger=d.get("token_count_at_trigger", 0),
            snapshot_id=d.get("snapshot_id", uuid.uuid4().hex[:12]),
            timestamp=d.get("timestamp", time.time()),
        )

    def to_readable_text(self) -> str:
        """Format messages into clean markdown transcript representation."""
        lines = [
            f"# Pre-Compaction Snapshot [{self.snapshot_id}]",
            f"- Trigger index: {self.trigger_index}",
            f"- Message count: {self.message_count}",
            f"- Token count at trigger: {self.token_count_at_trigger}",
            "",
            "## Transcript",
            "",
        ]
        for i, msg in enumerate(self.messages, start=1):
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            lines.append(f"### Turn {i} ({role})")
            if content:
                lines.append(str(content).strip())
            if msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    fn = tc.get("function", {})
                    fn_name = fn.get("name", "tool")
                    fn_args = fn.get("arguments", "")
                    lines.append(f"```tool_call:{fn_name}\n{fn_args}\n```")
            lines.append("")
        return "\n".join(lines).strip()


def get_default_snapshot_dir() -> Path:
    """Resolve default directory for pre-compaction snapshots."""
    try:
        from hermes_constants import get_hermes_home

        base = get_hermes_home()
    except Exception:
        base = Path.home() / ".hermes"
    d = base / "snapshots"
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_precompaction_snapshot(
    snapshot: CompactionSnapshot,
    snapshot_dir: Optional[Union[str, Path]] = None,
    session_id: Optional[str] = None,
) -> Path:
    """Persist pre-compaction snapshot to disk as a re-readable virtual file."""
    target_dir = Path(snapshot_dir) if snapshot_dir else get_default_snapshot_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    prefix = f"{session_id}_" if session_id else ""
    file_name = f"{prefix}snapshot_{snapshot.snapshot_id}.json"
    snapshot_path = target_dir / file_name
    snapshot_path.write_text(json.dumps(snapshot.to_dict(), indent=2), encoding="utf-8")
    return snapshot_path


def read_precompaction_snapshot(snapshot_path: Union[str, Path]) -> CompactionSnapshot:
    """Read a pre-compaction snapshot from disk."""
    p = Path(snapshot_path)
    data = json.loads(p.read_text(encoding="utf-8"))
    return CompactionSnapshot.from_dict(data)


def read_precompaction_snapshot_text(snapshot_path: Union[str, Path]) -> str:
    """Read pre-compaction snapshot as human/agent-readable transcript."""
    snap = read_precompaction_snapshot(snapshot_path)
    return snap.to_readable_text()


class ParallelCompactor:
    """Orchestrates early-triggered background context compaction and safe turn swaps."""

    def __init__(
        self,
        config: Optional[ParallelCompactionConfig] = None,
        executor: Optional[concurrent.futures.Executor] = None,
        session_id: Optional[str] = None,
    ) -> None:
        self.config = config or ParallelCompactionConfig()
        self._executor = executor
        self._session_id = session_id
        self._state = CompactionState.IDLE
        self._future: Optional[concurrent.futures.Future[List[Dict[str, Any]]]] = None
        self._snapshot: Optional[CompactionSnapshot] = None
        self._snapshot_path: Optional[Path] = None
        self._compacted_prefix: Optional[List[Dict[str, Any]]] = None
        self._last_error: Optional[str] = None
        self._swap_count: int = 0

    @property
    def session_id(self) -> Optional[str]:
        return self._session_id

    @session_id.setter
    def session_id(self, val: Optional[str]) -> None:
        self._session_id = val

    @property
    def state(self) -> CompactionState:
        return self._state

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    @property
    def swap_count(self) -> int:
        return self._swap_count

    @property
    def snapshot(self) -> Optional[CompactionSnapshot]:
        return self._snapshot

    @property
    def snapshot_path(self) -> Optional[Path]:
        return self._snapshot_path

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
        if self.config.save_snapshot:
            try:
                self._snapshot_path = write_precompaction_snapshot(
                    self._snapshot,
                    snapshot_dir=self.config.snapshot_dir,
                    session_id=self._session_id,
                )
            except Exception as exc:
                self._snapshot_path = None
        else:
            self._snapshot_path = None

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

    def read_snapshot(
        self, snapshot_path: Optional[Union[str, Path]] = None
    ) -> Optional[CompactionSnapshot]:
        """Read a pre-compaction snapshot from disk or memory."""
        if snapshot_path is not None:
            return read_precompaction_snapshot(snapshot_path)
        if self._snapshot is not None:
            return self._snapshot
        if self._snapshot_path is not None and self._snapshot_path.exists():
            return read_precompaction_snapshot(self._snapshot_path)
        return None

    def read_snapshot_text(
        self, snapshot_path: Optional[Union[str, Path]] = None
    ) -> Optional[str]:
        """Read snapshot as formatted text transcript."""
        if snapshot_path is not None:
            return read_precompaction_snapshot_text(snapshot_path)
        snap = self.read_snapshot()
        if snap is not None:
            return snap.to_readable_text()
        return None

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
        self._snapshot_path = None
        self._compacted_prefix = None
        self._last_error = None
