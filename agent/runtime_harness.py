"""Agent runtime harness with recovery, pause/resume, and kill-switch controls.

Implements #3279: Control plane supervising the execution loop with spiral guardrails,
checkpoint serialization, and kill-switch safety primitives.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional


class HarnessStatus(str, Enum):
    RUNNING = "running"
    PAUSED = "paused"
    KILLED = "killed"
    COMPLETED = "completed"


class HarnessAction(str, Enum):
    PROCEED = "proceed"
    PAUSE = "pause"
    KILL = "kill"
    RECOVER = "recover"


@dataclass
class HarnessPolicy:
    """Configurable execution constraints for the runtime harness."""

    max_consecutive_tool_calls: int = 50
    max_unproductive_turns: int = 10
    max_wall_time_seconds: float = 3600.0
    kill_on_unhandled_spiral: bool = True
    max_tool_failures_before_recovery: int = 3
    audit_file_path: Optional[str] = None


@dataclass
class HarnessEvent:
    event_type: str
    timestamp: float
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class HarnessDecision:
    action: HarnessAction
    reason: str
    can_resume: bool = True


class AgentRuntimeHarness:
    """Supervises agent execution loop, providing pause, resume, checkpointing, and kill-switch."""

    def __init__(
        self,
        session_id: str,
        policy: Optional[HarnessPolicy] = None,
        on_event: Optional[Callable[[HarnessEvent], None]] = None,
    ):
        self.session_id = session_id
        self.policy = policy or HarnessPolicy()
        self.status = HarnessStatus.RUNNING
        self.started_at = time.time()
        self.tool_call_streak = 0
        self.unproductive_turns = 0
        self.events: list[HarnessEvent] = []
        self.checkpoints: list[dict[str, Any]] = []
        self.on_event = on_event
        self._kill_requested = False

    def _emit(self, event_type: str, details: dict[str, Any]) -> None:
        event = HarnessEvent(
            event_type=event_type, timestamp=time.time(), details=details
        )
        self.events.append(event)
        if self.policy.audit_file_path:
            try:
                with open(self.policy.audit_file_path, "a", encoding="utf-8") as f:
                    entry = {
                        "timestamp": event.timestamp,
                        "session_id": self.session_id,
                        "event_type": event_type,
                        "details": details,
                    }
                    f.write(json.dumps(entry) + "\n")
            except Exception:
                pass
        if self.on_event:
            try:
                self.on_event(event)
            except Exception:
                pass

    def recover_tool_failure(
        self,
        tool_name: str,
        error: str,
        consecutive_failures: int = 1,
    ) -> HarnessDecision:
        """Trigger recovery policy on repeated tool failures."""
        args_summary = f"{tool_name} failed ({consecutive_failures}x): {error[:100]}"
        self._emit(
            "harness.recover",
            {
                "tool_name": tool_name,
                "args_summary": args_summary,
                "reason": f"Repeated failure of {tool_name}",
                "consecutive_failures": consecutive_failures,
            },
        )
        if consecutive_failures >= self.policy.max_tool_failures_before_recovery:
            return HarnessDecision(
                action=HarnessAction.RECOVER,
                reason=f"Exceeded failure threshold for {tool_name}; initiating fallback/recovery",
                can_resume=True,
            )
        return HarnessDecision(
            action=HarnessAction.PROCEED,
            reason="Failure logged; within retry budget",
            can_resume=True,
        )

    def pause(self, reason: str = "user_pause") -> HarnessDecision:
        """Pause session execution safely."""
        self.status = HarnessStatus.PAUSED
        self._emit("harness.pause", {"reason": reason})
        return HarnessDecision(
            action=HarnessAction.PAUSE, reason=reason, can_resume=True
        )

    def resume(self) -> HarnessDecision:
        """Resume paused session execution."""
        if self.status == HarnessStatus.KILLED:
            return HarnessDecision(
                action=HarnessAction.KILL,
                reason="Cannot resume killed session",
                can_resume=False,
            )
        self.status = HarnessStatus.RUNNING
        self._emit("harness.resume", {})
        return HarnessDecision(
            action=HarnessAction.PROCEED, reason="Session resumed", can_resume=True
        )

    def kill(self, reason: str = "kill_switch_activated") -> HarnessDecision:
        """Immediately terminate all active executions and prevent further operations."""
        self._kill_requested = True
        self.status = HarnessStatus.KILLED
        self._emit("harness.kill", {"reason": reason})
        return HarnessDecision(
            action=HarnessAction.KILL, reason=reason, can_resume=False
        )

    def check_pre_execution(self, tool_name: str, args: dict[str, Any]) -> HarnessDecision:
        """Consult harness before executing a tool."""
        if self._kill_requested or self.status == HarnessStatus.KILLED:
            return HarnessDecision(
                action=HarnessAction.KILL,
                reason="Execution halted by kill-switch",
                can_resume=False,
            )

        if self.status == HarnessStatus.PAUSED:
            return HarnessDecision(
                action=HarnessAction.PAUSE,
                reason="Execution is currently paused",
                can_resume=True,
            )

        # Wall clock limit check
        elapsed = time.time() - self.started_at
        if elapsed > self.policy.max_wall_time_seconds:
            self.pause("Max session wall-clock time exceeded")
            return HarnessDecision(
                action=HarnessAction.PAUSE,
                reason=f"Exceeded max wall time {self.policy.max_wall_time_seconds}s",
                can_resume=True,
            )

        # Consecutive tool streak check
        self.tool_call_streak += 1
        if self.tool_call_streak > self.policy.max_consecutive_tool_calls:
            self.pause(f"Tool call streak limit {self.policy.max_consecutive_tool_calls} reached")
            return HarnessDecision(
                action=HarnessAction.PAUSE,
                reason="Consecutive tool streak limit reached; pausing for review",
                can_resume=True,
            )

        return HarnessDecision(action=HarnessAction.PROCEED, reason="OK", can_resume=True)

    def record_turn_result(
        self,
        has_productive_output: bool,
        tool_name: Optional[str] = None,
        failed: bool = False,
    ) -> HarnessDecision:
        """Record turn outcome and track unproductive spirals."""
        if not has_productive_output or failed:
            self.unproductive_turns += 1
        else:
            self.unproductive_turns = 0
            self.tool_call_streak = 0

        if self.unproductive_turns >= self.policy.max_unproductive_turns:
            if self.policy.kill_on_unhandled_spiral:
                return self.kill(f"Max unproductive turns ({self.policy.max_unproductive_turns}) reached: spiral detected")
            return self.pause("Max unproductive turns reached")

        return HarnessDecision(action=HarnessAction.PROCEED, reason="OK", can_resume=True)

    def create_checkpoint(self, state_snapshot: dict[str, Any]) -> dict[str, Any]:
        """Save a serializable state checkpoint."""
        checkpoint = {
            "session_id": self.session_id,
            "status": self.status.value,
            "timestamp": time.time(),
            "tool_call_streak": self.tool_call_streak,
            "unproductive_turns": self.unproductive_turns,
            "state_snapshot": state_snapshot,
        }
        self.checkpoints.append(checkpoint)
        self._emit("harness.checkpoint", {"index": len(self.checkpoints) - 1})
        return checkpoint

    def restore_checkpoint(self, checkpoint: dict[str, Any]) -> dict[str, Any]:
        """Restore harness state from checkpoint."""
        self.tool_call_streak = checkpoint.get("tool_call_streak", 0)
        self.unproductive_turns = checkpoint.get("unproductive_turns", 0)
        self.status = HarnessStatus.RUNNING
        self._emit("harness.restore", {"timestamp": checkpoint.get("timestamp")})
        return checkpoint.get("state_snapshot", {})
