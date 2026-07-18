# -*- coding: utf-8 -*-
"""Recovery strategy dispatcher for tool failures (issue #1027, child of #1019).

Third slice of PALADIN-style execution-level tool-failure recovery. The first
slice (:mod:`tools.tool_failure_classifier`) turns a raw tool error into a
structured :class:`~tools.tool_failure_classifier.ToolFailureClassification`.
This module maps that classification onto a concrete **recovery action** — a
retry-with-backoff, an argument fix, a tool switch, a target verification, or a
surfaced blocker — so the agent takes a targeted next step instead of blindly
re-running the same failing call.

Pipeline::

    classify (slice 1)  ->  dispatch recovery (this module)  ->  guidance seam

The public entry point :func:`recover_from_failure` classifies *and* dispatches
in one call, and :func:`maybe_append_recovery_guidance` is the runtime seam that
``run_agent`` invokes after a failed tool call (config-gated, off by default).

Design goals (matching ``tools/tool_failure_classifier.py`` and
``agent/tool_guardrails.py``):

* Pure functions + frozen dataclasses; **no side effects on import**.
* Standard library only; full type hints; ``from __future__ import annotations``.
* Deterministic, table-driven ``category -> strategy`` mapping, extensible at
  runtime via :func:`register_strategy`.
* Escalation is monotone: as the same call keeps failing, retryable strategies
  give way to a tool switch and finally a surfaced blocker, so a recovery
  directive can never itself drive an unbounded retry spiral.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any

from tools.tool_failure_classifier import (
    ToolFailureCategory,
    ToolFailureClassification,
    classify_tool_failure,
)

__all__ = [
    "RecoveryStrategy",
    "RecoveryAction",
    "dispatch_recovery",
    "recover_from_failure",
    "register_strategy",
    "backoff_seconds_for",
    "maybe_append_recovery_guidance",
    "RECOVERY_GUIDANCE_PREFIX",
]


class RecoveryStrategy(str, Enum):
    """Concrete recovery actions a failure can be dispatched to."""

    retry = "retry"  # transient, plain retry may work
    retry_with_backoff = "retry_with_backoff"  # transient, wait then retry
    fix_arguments = "fix_arguments"  # deterministic arg error, correct then call
    verify_target = "verify_target"  # target missing, look it up first
    switch_tool = "switch_tool"  # this tool can't do it, use another
    surface_blocker = "surface_blocker"  # external blocker, report it
    escalate = "escalate"  # repeated failure, stop and change strategy


# Number of consecutive same-call failures after which even a retryable category
# stops retrying: first the tool is switched, then the blocker is surfaced. This
# guarantees a recovery directive cannot sustain a retry spiral on its own.
_SWITCH_AFTER = 3
_SURFACE_AFTER = 5


# Default category -> strategy table. ``should_retry`` on the classification is
# authoritative for *whether* to retry; this table selects the *shape* of the
# recovery when a retry is not the answer (or when a smarter retry — backoff —
# is warranted). First lookup wins; extend at runtime with ``register_strategy``.
_CATEGORY_STRATEGY: dict[ToolFailureCategory, RecoveryStrategy] = {
    ToolFailureCategory.tool_unavailable: RecoveryStrategy.switch_tool,
    ToolFailureCategory.invalid_arguments: RecoveryStrategy.fix_arguments,
    ToolFailureCategory.not_found: RecoveryStrategy.verify_target,
    ToolFailureCategory.permission_denied: RecoveryStrategy.surface_blocker,
    ToolFailureCategory.rate_limited: RecoveryStrategy.retry_with_backoff,
    ToolFailureCategory.transient_network: RecoveryStrategy.retry_with_backoff,
    ToolFailureCategory.timeout: RecoveryStrategy.retry_with_backoff,
    ToolFailureCategory.unexpected_output: RecoveryStrategy.retry,
    ToolFailureCategory.persistent_error: RecoveryStrategy.switch_tool,
    ToolFailureCategory.unknown: RecoveryStrategy.surface_blocker,
}

# Short imperative directives per strategy. Kept concise and structured so the
# agent loop (or a policy interceptor) can surface a concrete alternative action.
_STRATEGY_DIRECTIVE: dict[RecoveryStrategy, str] = {
    RecoveryStrategy.retry: (
        "retry the call once; if it fails again, change the request or switch tools"
    ),
    RecoveryStrategy.retry_with_backoff: (
        "wait for the backoff window, then retry; reduce request frequency if it recurs"
    ),
    RecoveryStrategy.fix_arguments: (
        "do not retry unchanged — correct the arguments (required fields, allowed "
        "values, exact-match text) before calling again"
    ),
    RecoveryStrategy.verify_target: (
        "verify the target exists first (list the directory or search) before "
        "repeating the lookup"
    ),
    RecoveryStrategy.switch_tool: (
        "this tool cannot complete the task — switch to an alternative tool or "
        "resolve the missing dependency"
    ),
    RecoveryStrategy.surface_blocker: (
        "this is an external blocker — report it after one diagnostic attempt "
        "instead of retrying"
    ),
    RecoveryStrategy.escalate: (
        "this call has failed repeatedly — stop retrying it and change strategy or "
        "report the blocker"
    ),
}

_BASE_BACKOFF_SECONDS = 1.0
_MAX_BACKOFF_SECONDS = 30.0


@dataclass(frozen=True)
class RecoveryAction:
    """A concrete recovery decision for a classified tool failure."""

    category: ToolFailureCategory
    strategy: RecoveryStrategy
    directive: str
    should_retry: bool
    backoff_seconds: float | None = None
    tool_name: str = ""

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "category": self.category.value,
            "strategy": self.strategy.value,
            "directive": self.directive,
            "should_retry": self.should_retry,
            "tool_name": self.tool_name,
        }
        if self.backoff_seconds is not None:
            data["backoff_seconds"] = self.backoff_seconds
        return data


def register_strategy(
    category: ToolFailureCategory, strategy: RecoveryStrategy
) -> None:
    """Override the recovery strategy for a failure category (runtime-extensible)."""
    _CATEGORY_STRATEGY[category] = strategy


def backoff_seconds_for(consecutive_count: int) -> float:
    """Deterministic exponential backoff, capped, for retry-with-backoff actions.

    ``consecutive_count`` is the number of prior consecutive failures of the same
    call this turn (0 for the first failure). No jitter — deterministic so the
    behavior is testable and reproducible.
    """
    exponent = max(0, int(consecutive_count))
    delay = _BASE_BACKOFF_SECONDS * (2 ** exponent)
    return float(min(delay, _MAX_BACKOFF_SECONDS))


def dispatch_recovery(
    classification: ToolFailureClassification,
    *,
    tool_name: str = "",
    consecutive_count: int = 0,
) -> RecoveryAction:
    """Map a failure classification to a concrete :class:`RecoveryAction`.

    Escalation: once the same call has failed ``_SWITCH_AFTER`` times a
    retryable category is redirected to :attr:`RecoveryStrategy.switch_tool`, and
    after ``_SURFACE_AFTER`` failures to :attr:`RecoveryStrategy.escalate`, so a
    recovery directive never sustains an unbounded retry.
    """
    category = classification.category
    strategy = _CATEGORY_STRATEGY.get(category, RecoveryStrategy.surface_blocker)
    should_retry = bool(classification.should_retry)

    # Monotone escalation for retryable categories that keep failing.
    if should_retry and consecutive_count >= _SURFACE_AFTER:
        strategy = RecoveryStrategy.escalate
        should_retry = False
    elif should_retry and consecutive_count >= _SWITCH_AFTER:
        strategy = RecoveryStrategy.switch_tool
        should_retry = False

    backoff: float | None = None
    if should_retry and strategy is RecoveryStrategy.retry_with_backoff:
        backoff = backoff_seconds_for(consecutive_count)

    directive = _STRATEGY_DIRECTIVE.get(strategy, _STRATEGY_DIRECTIVE[RecoveryStrategy.surface_blocker])
    return RecoveryAction(
        category=category,
        strategy=strategy,
        directive=directive,
        should_retry=should_retry,
        backoff_seconds=backoff,
        tool_name=tool_name,
    )


def recover_from_failure(
    tool_name: str,
    error: str = "",
    *,
    exit_code: int | None = None,
    consecutive_count: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> RecoveryAction:
    """Classify a failed tool call and dispatch it to a recovery action.

    This is the single structured entry point that wires a raw failure into a
    recovery decision. Arguments mirror
    :func:`tools.tool_failure_classifier.classify_tool_failure`.
    """
    classification = classify_tool_failure(
        tool_name,
        error,
        exit_code=exit_code,
        consecutive_count=consecutive_count,
        stdout=stdout,
        stderr=stderr,
    )
    return dispatch_recovery(
        classification,
        tool_name=tool_name,
        consecutive_count=consecutive_count,
    )


# ---------------------------------------------------------------------------
# Runtime seam (config-gated, invoked from run_agent after a failed tool call)
# ---------------------------------------------------------------------------

RECOVERY_GUIDANCE_PREFIX = "Recovery strategy"


def _format_recovery_guidance(action: RecoveryAction) -> str:
    """Render a single structured guidance line for a recovery action."""
    parts = [
        f"{RECOVERY_GUIDANCE_PREFIX}: {action.strategy.value}",
        f"category={action.category.value}",
        f"retry={'yes' if action.should_retry else 'no'}",
    ]
    if action.backoff_seconds is not None:
        parts.append(f"backoff={action.backoff_seconds:g}s")
    line = "; ".join(parts)
    return f"\n\n[{line}. {action.directive}]"


def _terminal_exit_code(result: str) -> int | None:
    """Best-effort extraction of a terminal tool's exit code from its JSON result."""
    try:
        parsed = json.loads(result)
    except (ValueError, TypeError):
        return None
    if isinstance(parsed, dict):
        code = parsed.get("exit_code")
        if isinstance(code, int):
            return code
    return None


def maybe_append_recovery_guidance(
    result: str | None,
    tool_name: str,
    *,
    failed: bool,
    enabled: bool,
    consecutive_count: int = 0,
    exit_code: int | None = None,
) -> str:
    """Runtime seam: append a structured recovery directive to a failed result.

    Returns ``result`` **byte-for-byte unchanged** unless ``enabled`` and
    ``failed`` are both true, so the default (disabled) path is a pure pass
    through and introduces no behavior change. Never raises — a dispatch error
    degrades to the original result.

    For terminal failures the exit code is auto-extracted from the JSON result
    when ``exit_code`` is not supplied, so the caller stays minimal.
    """
    text = result or ""
    if not enabled or not failed:
        return text
    if exit_code is None and tool_name == "terminal":
        exit_code = _terminal_exit_code(text)
    try:
        action = recover_from_failure(
            tool_name,
            error=text[:2000],
            exit_code=exit_code,
            consecutive_count=consecutive_count,
        )
    except Exception:
        return text
    return text + _format_recovery_guidance(action)
