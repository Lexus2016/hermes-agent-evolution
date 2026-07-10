"""Error recovery ladder — increment 1 of #826.

Structured failure classification and recovery strategy mapping for the agent
loop.  Addresses 122+ tool failures from the 2026-07-08 introspection cycle:
terminal (64 failures/33 sessions), read_file (40/16), search_files (10/11),
patch (8).

This increment delivers the **classification + strategy core** — a standalone
module that classifies tool/provider errors into broad categories and maps
them to recovery actions.  Wiring into the agent loop is a separate increment.

Existing circuit breakers (MCP per-server, kanban dispatcher) are
domain-specific; this module provides the general-purpose error taxonomy
that those systems and the agent loop can share.

Public API
----------
    from agent.error_recovery import classify_error, recommend_action

    err_class = classify_error("Connection timed out")
    action = recommend_action(err_class, attempt_number=2)
    if action == RecoveryAction.RETRY_WITH_BACKOFF:
        ...
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Dict, List, Optional, Pattern, Tuple

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class ErrorClass(IntEnum):
    """Broad classification of a tool/provider failure."""

    TRANSIENT = 0  # network blip, rate limit, temporary unavailability
    PERMANENT = 1  # auth failure, validation error, file not found
    CRITICAL = 2  # unrepairable system state, infinite loop detected
    UNKNOWN = 3  # could not classify — treat conservatively


class RecoveryAction(IntEnum):
    """Recommended recovery action for a given (ErrorClass, attempt)."""

    RETRY = 0  # immediate retry (same call, same params)
    RETRY_WITH_BACKOFF = 1  # retry after delay with exponential backoff
    FALLBACK = 2  # use an alternative tool/approach
    ESCALATE = 3  # hand off to user/human with context
    ABORT = 4  # stop — further attempts are futile or harmful


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------

# Pattern → ErrorClass mapping.  Ordered by specificity: more specific
# patterns (rate limit, auth) are checked before generic ones (timeout).
_ERROR_PATTERNS: List[Tuple[Pattern[str], ErrorClass]] = [
    # --- Rate limiting (transient — retry after backoff) ---
    (
        re.compile(r"rate.?limit|429|too many requests|throttl", re.I),
        ErrorClass.TRANSIENT,
    ),
    (re.compile(r"quota|usage.?limit|credit", re.I), ErrorClass.TRANSIENT),
    # --- Auth/permission (permanent — retrying won't help) ---
    (
        re.compile(
            r"401|403|unauthor|forbidden|permission.?denied|access.?denied", re.I
        ),
        ErrorClass.PERMANENT,
    ),
    (
        re.compile(r"api.?key|token.*(invalid|expired|revoked)", re.I),
        ErrorClass.PERMANENT,
    ),
    # --- Not found (permanent — the resource doesn't exist) ---
    (
        re.compile(
            r"404|not.?found|no such (file|directory|module)|does not exist", re.I
        ),
        ErrorClass.PERMANENT,
    ),
    # --- Validation/bad request (permanent — the request itself is wrong) ---
    (
        re.compile(
            r"400|bad.?request|validation|invalid.*(param|argument|input|format)", re.I
        ),
        ErrorClass.PERMANENT,
    ),
    (
        re.compile(r"syntax.?error|parse.?error|json.?decode", re.I),
        ErrorClass.PERMANENT,
    ),
    # --- Timeout/transient network (transient — retry with backoff) ---
    (re.compile(r"timeout|timed.?out|deadline.?exceeded", re.I), ErrorClass.TRANSIENT),
    (
        re.compile(
            r"connection.*(refused|reset|closed|abort)|ECONNREFUSED|ECONNRESET", re.I
        ),
        ErrorClass.TRANSIENT,
    ),
    (
        re.compile(
            r"5\d{2}\b|internal.?server|service.?unavailable|bad.?gateway|gateway.?timeout",
            re.I,
        ),
        ErrorClass.TRANSIENT,
    ),
    (
        re.compile(r"temporar|transient|retryable|retry.?again", re.I),
        ErrorClass.TRANSIENT,
    ),
    # --- Critical system state ---
    (
        re.compile(r"out.?of.?memory|OOM|disk.?full|no space left", re.I),
        ErrorClass.CRITICAL,
    ),
    (
        re.compile(
            r"infinite.?loop|recursion.?error|stack.?overflow|maximum.?recursion", re.I
        ),
        ErrorClass.CRITICAL,
    ),
]


@dataclass
class ClassifiedError:
    """Result of classifying an error."""

    error_class: ErrorClass
    matched_pattern: str = ""
    tool_name: str = ""
    error_message: str = ""

    @property
    def is_transient(self) -> bool:
        return self.error_class == ErrorClass.TRANSIENT

    @property
    def is_permanent(self) -> bool:
        return self.error_class == ErrorClass.PERMANENT

    def to_dict(self) -> Dict[str, str]:
        return {
            "error_class": self.error_class.name,
            "matched_pattern": self.matched_pattern,
            "tool_name": self.tool_name,
            "error_message": self.error_message,
        }


def classify_error(
    error_message: str,
    tool_name: str = "",
    status_code: Optional[int] = None,
) -> ClassifiedError:
    """Classify a tool/provider error into a broad category.

    Pattern-matches the error message against known error signatures.
    If a status code is provided, it takes precedence for HTTP-status
    ranges (429 → transient, 5xx → transient, 4xx → permanent).

    Returns a ``ClassifiedError`` with the best-guess class.  Unknown
    errors default to ``ErrorClass.UNKNOWN`` (treated conservatively).
    """
    # Status-code fast path
    if status_code is not None:
        if status_code == 429:
            return ClassifiedError(
                ErrorClass.TRANSIENT, f"HTTP {status_code}", tool_name, error_message
            )
        if 500 <= status_code < 600:
            return ClassifiedError(
                ErrorClass.TRANSIENT, f"HTTP {status_code}", tool_name, error_message
            )
        if 400 <= status_code < 500:
            return ClassifiedError(
                ErrorClass.PERMANENT, f"HTTP {status_code}", tool_name, error_message
            )

    # Pattern matching
    for pattern, err_class in _ERROR_PATTERNS:
        match = pattern.search(error_message)
        if match:
            return ClassifiedError(err_class, match.group(0), tool_name, error_message)

    return ClassifiedError(ErrorClass.UNKNOWN, "", tool_name, error_message)


# ---------------------------------------------------------------------------
# Recovery strategy
# ---------------------------------------------------------------------------

# (ErrorClass, max_attempts) → RecoveryAction
# Strategy: transient → retry with backoff up to max, then fallback;
#           permanent → fallback immediately (retrying won't fix the request);
#           critical → escalate; unknown → retry once, then escalate.
_DEFAULT_STRATEGY: Dict[ErrorClass, List[RecoveryAction]] = {
    ErrorClass.TRANSIENT: [
        RecoveryAction.RETRY,
        RecoveryAction.RETRY_WITH_BACKOFF,
        RecoveryAction.RETRY_WITH_BACKOFF,
        RecoveryAction.FALLBACK,
    ],
    ErrorClass.PERMANENT: [
        RecoveryAction.FALLBACK,
        RecoveryAction.ESCALATE,
    ],
    ErrorClass.CRITICAL: [
        RecoveryAction.ESCALATE,
    ],
    ErrorClass.UNKNOWN: [
        RecoveryAction.RETRY,
        RecoveryAction.ESCALATE,
    ],
}


def recommend_action(
    error_class: ErrorClass,
    attempt_number: int = 1,
    strategy: Optional[Dict[ErrorClass, List[RecoveryAction]]] = None,
) -> RecoveryAction:
    """Recommend a recovery action for a classified error.

    ``attempt_number`` is 1-indexed: the first failure is attempt 1.
    The strategy is a mapping from ``ErrorClass`` to an ordered list of
    actions; the action at ``min(attempt_number - 1, len(actions) - 1)``
    is returned.  Past the last entry, the final action is returned
    (usually ESCALATE or FALLBACK).
    """
    strat = strategy or _DEFAULT_STRATEGY
    if error_class in strat:
        actions = strat[error_class]
    else:
        actions = strat.get(ErrorClass.UNKNOWN) or _DEFAULT_STRATEGY[ErrorClass.UNKNOWN]
    if not actions:
        return RecoveryAction.ESCALATE
    idx = min(attempt_number - 1, len(actions) - 1)
    return actions[idx]


def should_retry(
    error_class: ErrorClass,
    attempt_number: int = 1,
    max_attempts: int = 3,
    strategy: Optional[Dict[ErrorClass, List[RecoveryAction]]] = None,
) -> bool:
    """Convenience: True if the recommended action is a retry variant."""
    if attempt_number >= max_attempts:
        return False
    action = recommend_action(error_class, attempt_number, strategy)
    return action in (RecoveryAction.RETRY, RecoveryAction.RETRY_WITH_BACKOFF)
