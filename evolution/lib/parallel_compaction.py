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
import json
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


class CompactionState(Enum):
    """Lifecycle state of an asynchronous compaction pass."""

    IDLE = "idle"
    COMPACTING = "compacting"
    READY_TO_SWAP = "ready_to_swap"
    SWAPPED = "swapped"
    FAILED = "failed"


@dataclass
class ParallelCompactionConfig:
    """Configuration for parallel context compaction.

    Attributes:
        enabled: Master toggle for early-trigger parallel compaction.
        hard_token_limit: Upper bound context capacity for the model.
        headroom_ratio: Fraction of hard limit to reserve as early-trigger headroom.
        explicit_headroom_tokens: Optional explicit token count for headroom override.
        preserve_recent_count: Number of recent messages to exclude from compression.
    """

    enabled: bool = True
    hard_token_limit: int = 100000
    headroom_ratio: float = 0.15
    explicit_headroom_tokens: Optional[int] = None
    preserve_recent_count: int = 4

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
        return cls(**{k: d[k] for k in d if k in cls.__dataclass_fields__})


@dataclass
class CompactionSnapshot:
    """Snapshot captured when background compaction was triggered."""

    trigger_index: int
    message_count: int
    messages: List[Dict[str, Any]]
    token_count_at_trigger: int


def _default_snapshot_dir() -> Path:
    return Path.home() / ".hermes" / "compaction" / "snapshots"


def write_precompaction_snapshot(
    messages: List[Dict[str, Any]], snapshot_dir: Optional[str] = None
) -> str:
    """Write a pre-compaction snapshot to a re-readable JSON file (#2471).

    Returns the absolute path so the agent can later re-read dropped context via
    :func:`read_precompaction_snapshot` instead of re-running the original tool
    calls.
    """
    directory = Path(snapshot_dir) if snapshot_dir else _default_snapshot_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"snapshot-{int(time.time() * 1000)}.json"
    payload = {"messages": messages, "captured_at_unix_ms": int(time.time() * 1000)}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


def read_precompaction_snapshot(path: str) -> List[Dict[str, Any]]:
    """Re-read a snapshot written by :func:`write_precompaction_snapshot`."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return data["messages"]


class ParallelCompactor:
    """Orchestrates early-triggered background context compaction and safe turn swaps."""

    def __init__(
        self,
        config: Optional[ParallelCompactionConfig] = None,
        executor: Optional[concurrent.futures.Executor] = None,
        snapshot_dir: Optional[str] = None,
    ) -> None:
        self.config = config or ParallelCompactionConfig()
        self._executor = executor
        self._snapshot_dir = snapshot_dir
        self._snapshot_path: Optional[str] = None
        self._state = CompactionState.IDLE
        self._future: Optional[concurrent.futures.Future[List[Dict[str, Any]]]] = None
        self._snapshot: Optional[CompactionSnapshot] = None
        self._compacted_prefix: Optional[List[Dict[str, Any]]] = None
        self._last_error: Optional[str] = None
        self._swap_count: int = 0

    @property
    def snapshot_path(self) -> Optional[str]:
        return self._snapshot_path

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
        try:
            self._snapshot_path = write_precompaction_snapshot(
                snapshot_messages, self._snapshot_dir
            )
        except OSError:
            # Best-effort recovery aid: never block compaction on a snapshot write.
            self._snapshot_path = None
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

    def read_snapshot(
        self, path: Optional[str] = None
    ) -> Optional[List[Dict[str, Any]]]:
        """Re-read the pre-compaction snapshot for this pass (or an explicit path).

        Returns None when no snapshot was captured this pass and no path was given.
        """
        target = path or self._snapshot_path
        if target is None:
            return None
        return read_precompaction_snapshot(target)

    def reset(self) -> None:
        """Reset state for next cycle."""
        self._state = CompactionState.IDLE
        self._future = None
        self._snapshot = None
        self._snapshot_path = None
        self._compacted_prefix = None
        self._last_error = None
