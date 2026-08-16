# -*- coding: utf-8 -*-
"""Path-based runtime governance — execution-path policy checker (#2284).

Policies on Paths (arXiv:2603.16586, Kaptein/Khan/Podstavnychy — Kyvvu,
Mar 2026): a database read alone is fine; a database read followed by an
external email is a potential exfiltration event — and no inspection of
either step in isolation reveals this. Prompt-level instructions shift the
path distribution but don't evaluate it; static access control ignores
path entirely.

This module implements the first increment of path-based governance:

1. **Execution-path log** — a lightweight append-only log of tool calls in
   the current session: ``(timestamp, tool_name, args_summary, result_category)``.
   This is the "partial execution path" from the paper.

2. **Path-based policy checker** — before executing high-risk tools
   (``send_message``, ``create_tweet``, ``git push``, ``delete_*``),
   evaluate the recent path against a small rule set:
   - ``.env`` / secrets file read → any network tool call within N turns = **BLOCK**
   - ``write_file`` modifying a skill → ``run_validation`` not called within N turns = **STEER**
   - More than K destructive operations (delete, overwrite) in a session = **STEER**

3. **Pass/Steer/Block trichotomy** — maps to Hermes's existing
   human-in-the-loop escalation.

4. **Per-profile risk budget** — each profile gets a risk allocation;
   cumulative violation scores gate further autonomous actions.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

__all__ = [
    "PathEvent",
    "PathLog",
    "PathVerdict",
    "PathPolicyChecker",
    "RiskBudget",
]

# Verdict trichotomy (Pass / Steer / Block).
PASS = "pass"
STEER = "steer"
BLOCK = "block"

# High-risk tools that trigger path evaluation before execution.
HIGH_RISK_TOOLS = (
    "send_message",
    "create_tweet",
    "git_push",
    "delete_file",
    "delete_directory",
    "network_request",
    "send_email",
)

# Network tools (for the exfiltration rule).
NETWORK_TOOLS = (
    "web_search",
    "web_extract",
    "send_message",
    "create_tweet",
    "send_email",
    "network_request",
    "git_push",
)

# Secrets files whose read followed by a network call is an exfiltration signal.
SECRETS_FILE_MARKERS = (
    ".env",
    "secrets",
    "credentials",
    "token",
    "password",
    "api_key",
)

# Destructive tools (for the destructive-op-count rule).
DESTRUCTIVE_TOOLS = ("delete_file", "delete_directory", "overwrite_file", "remove_file")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class PathEvent:
    """A single tool call in the execution path."""

    tool_name: str
    args_summary: str = ""
    result_category: str = ""
    timestamp: str = ""

    def __post_init__(self) -> None:
        if not self.timestamp:
            self.timestamp = _now_iso()

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "PathEvent":
        return cls(
            tool_name=str(d.get("tool_name", "")),
            args_summary=str(d.get("args_summary", "")),
            result_category=str(d.get("result_category", "")),
            timestamp=str(d.get("timestamp", "")),
        )


class PathLog:
    """Append-only execution-path log for the current session.

    Maintains an in-memory ring buffer of recent :class:`PathEvent` entries
    (bounded to ``max_entries``) plus an optional on-disk append-only log
    for audit. The in-memory buffer is what the policy checker evaluates.
    """

    def __init__(
        self, max_entries: int = 200, log_path: Optional[Path | str] = None
    ) -> None:
        self.max_entries = max_entries
        self._events: List[PathEvent] = []
        self.log_path = Path(log_path) if log_path else None

    def record(
        self,
        tool_name: str,
        args_summary: str = "",
        result_category: str = "",
    ) -> PathEvent:
        """Append a tool call to the path log."""
        event = PathEvent(
            tool_name=tool_name,
            args_summary=args_summary,
            result_category=result_category,
        )
        self._events.append(event)
        if len(self._events) > self.max_entries:
            self._events = self._events[-self.max_entries :]
        if self.log_path is not None:
            self._append_disk(event)
        return event

    def _append_disk(self, event: PathEvent) -> None:
        path = self.log_path
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(event.to_dict()) + "\n")
        except OSError as exc:
            logger.debug("path-log disk append failed: %s", exc)

    def recent(self, n: Optional[int] = None) -> List[PathEvent]:
        """Return the most recent ``n`` events (all if ``n`` is None)."""
        if n is None:
            return list(self._events)
        return self._events[-n:]

    def clear(self) -> None:
        """Clear the in-memory buffer (session boundary)."""
        self._events = []

    def __len__(self) -> int:
        return len(self._events)


@dataclass
class PathVerdict:
    """Result of evaluating the recent path against the policy rules."""

    tool_name: str
    verdict: str  # pass | steer | block
    rule: str = ""
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "PathVerdict":
        return cls(
            tool_name=str(d.get("tool_name", "")),
            verdict=str(d.get("verdict", PASS)),
            rule=str(d.get("rule", "")),
            reason=str(d.get("reason", "")),
        )


class PathPolicyChecker:
    """Evaluate the recent execution path against path-based policy rules.

    Pure and import-safe; the caller supplies the :class:`PathLog` and the
    tool name about to execute. Returns a :class:`PathVerdict` with the
    Pass/Steer/Block trichotomy.
    """

    def __init__(
        self,
        *,
        exfil_window: int = 5,
        validation_window: int = 5,
        destructive_limit: int = 3,
    ) -> None:
        self.exfil_window = exfil_window
        self.validation_window = validation_window
        self.destructive_limit = destructive_limit

    def evaluate(
        self, path_log: PathLog, tool_name: str, args_summary: str = ""
    ) -> PathVerdict:
        """Evaluate *tool_name* against the recent path in *path_log*."""
        tool_lower = (tool_name or "").lower()
        recent = path_log.recent()

        # Rule 1: secrets-file read → network call within N turns = BLOCK.
        if tool_lower in NETWORK_TOOLS:
            for event in recent[-self.exfil_window :]:
                if event.tool_name in ("read_file", "read", "cat", "view"):
                    if any(
                        marker in (event.args_summary or "").lower()
                        for marker in SECRETS_FILE_MARKERS
                    ):
                        return PathVerdict(
                            tool_name=tool_name,
                            verdict=BLOCK,
                            rule="secrets-read-then-network",
                            reason=(
                                f"Secrets file read ({event.args_summary}) followed by "
                                f"network tool '{tool_name}' within {self.exfil_window} turns — "
                                f"potential exfiltration."
                            ),
                        )

        # Rule 2: write_file modifying a skill → run_validation not called = STEER.
        if tool_lower in ("write_file", "patch", "edit_file"):
            if "skill" in (args_summary or "").lower():
                validation_called = any(
                    "run_validation" in e.tool_name or "validate" in e.tool_name
                    for e in recent[-self.validation_window :]
                )
                if not validation_called:
                    return PathVerdict(
                        tool_name=tool_name,
                        verdict=STEER,
                        rule="skill-write-without-validation",
                        reason=(
                            f"Modifying a skill ('{args_summary}') but run_validation was "
                            f"not called within {self.validation_window} turns — inject "
                            f"compliance reminder and request confirmation."
                        ),
                    )

        # Rule 3: more than K destructive ops in a session = STEER.
        if tool_lower in DESTRUCTIVE_TOOLS:
            destructive_count = sum(
                1 for e in recent if e.tool_name in DESTRUCTIVE_TOOLS
            )
            if destructive_count >= self.destructive_limit:
                return PathVerdict(
                    tool_name=tool_name,
                    verdict=STEER,
                    rule="destructive-op-limit",
                    reason=(
                        f"{destructive_count} destructive operations in this session "
                        f"(limit {self.destructive_limit}) — request confirmation."
                    ),
                )

        return PathVerdict(
            tool_name=tool_name, verdict=PASS, rule="", reason="Path OK."
        )


class RiskBudget:
    """Per-profile risk allocation that gates further autonomous actions.

    Each profile gets a risk budget. Cumulative violation scores (from
    Steer/Block verdicts) reduce the remaining budget; when it hits zero,
    further high-risk autonomous actions are blocked.
    """

    def __init__(self, budget: float = 10.0) -> None:
        self.budget = budget
        self.spent = 0.0

    def record_verdict(self, verdict: PathVerdict) -> float:
        """Record a verdict's cost and return the remaining budget.

        Steer costs 1.0, Block costs 3.0, Pass costs 0.0. The budget never
        goes below zero.
        """
        cost = {"pass": 0.0, "steer": 1.0, "block": 3.0}.get(verdict.verdict, 0.0)
        self.spent = min(self.budget, self.spent + cost)
        return self.remaining()

    def remaining(self) -> float:
        return max(0.0, self.budget - self.spent)

    @property
    def exhausted(self) -> bool:
        return self.remaining() <= 0.0
