"""Tool-level error classification and recovery hints.

Complements ``agent/error_classifier.py`` (which classifies *API-level*
errors for the main retry loop) by classifying *tool-level* errors that
surface when ``handle_function_call`` dispatches to a tool and the tool
raises an exception or returns a structured ``{"error": ...}`` result.

The classification is consumed by ``model_tools.handle_function_call`` to
enrich the error string returned to the agent with a recovery hint —
the model sees not just *what* went wrong but *what to try next*:
retry, try an alternative tool, check file paths, etc.

This module is deliberately minimal: it classifies, logs, and suggests.
The actual retry / fallback decision is made by the model reading the hint.
No automatic retries are attempted here — that would change the agent
loop's semantics and risk cache-breaking mid-conversation.
"""

from __future__ import annotations

import enum
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


# ── Error taxonomy ──────────────────────────────────────────────────────


class ToolErrorClass(enum.Enum):
    """Classification of a tool-level failure."""

    transient = "transient"  # timeout, resource busy — retry may help
    not_found = "not_found"  # file/path not found — check path
    permission = "permission"  # auth/permission denied — check credentials
    validation = "validation"  # bad arguments — fix the call args
    rate_limit = "rate_limit"  # tool-specific rate limit — backoff
    dependency = "dependency"  # missing system dependency — install/configure
    permanent = "permanent"  # structural failure — won't fix by retrying
    unknown = "unknown"  # couldn't classify


class RecoveryAction(enum.Enum):
    """Suggested recovery action for a tool error."""

    retry = "retry"  # retry the same call (transient)
    fix_args = "fix_args"  # fix the arguments and retry
    check_path = "check_path"  # verify the file/path exists
    check_credentials = "check_credentials"  # verify API key / permissions
    install_dependency = "install_dependency"  # install missing system tool
    use_alternative = "use_alternative"  # try a different tool or approach
    escalate = "escalate"  # surface to user, can't auto-recover
    abort = "abort"  # stop trying — permanent failure


@dataclass
class ToolFailure:
    """A classified tool failure with recovery context."""

    tool_name: str
    error_message: str
    error_class: ToolErrorClass
    recovery_action: RecoveryAction
    hint: str = ""  # human-readable recovery suggestion
    attempt_number: int = 1
    timestamp: float = 0.0  # set by caller if needed


# ── Pattern-based classifier ────────────────────────────────────────────

# Ordered (regex_pattern, error_class, recovery_action, hint) rules.
# First match wins. Patterns are case-insensitive.
# IMPORTANT: dependency patterns must be checked BEFORE not_found,
# because "command not found" contains the substring "not found".
_PATTERNS: list[tuple[re.Pattern, ToolErrorClass, RecoveryAction, str]] = [
    # Dependency missing (must precede not_found — "command not found"
    # would otherwise match the not_found pattern)
    (
        re.compile(
            r"command not found|not recognized|no module named|importerror|modulenotfound|executable.*not found",
            re.I,
        ),
        ToolErrorClass.dependency,
        RecoveryAction.install_dependency,
        "A required system dependency is missing. Install it and retry.",
    ),
    # Not found
    (
        re.compile(
            r"no such file|file not found|does not exist|not found|enoent", re.I
        ),
        ToolErrorClass.not_found,
        RecoveryAction.check_path,
        "The file or path was not found. Verify the path exists and is accessible.",
    ),
    # Permission
    (
        re.compile(r"permission denied|forbidden|unauthorized|403|access denied", re.I),
        ToolErrorClass.permission,
        RecoveryAction.check_credentials,
        "Permission was denied. Check file permissions or API credentials.",
    ),
    # Rate limit
    (
        re.compile(r"rate.?limit|too many requests|429|throttl", re.I),
        ToolErrorClass.rate_limit,
        RecoveryAction.retry,
        "Rate limited. Wait briefly before retrying, or reduce request frequency.",
    ),
    # Timeout / transient
    (
        re.compile(
            r"timeout|timed out|connection reset|temporarily unavailable|try again",
            re.I,
        ),
        ToolErrorClass.transient,
        RecoveryAction.retry,
        "A transient error occurred. Retrying the same call may succeed.",
    ),
    # Validation / bad args
    (
        re.compile(
            r"invalid|validation|bad request|wrong type|expected.*got|argument|param.*required|missing",
            re.I,
        ),
        ToolErrorClass.validation,
        RecoveryAction.fix_args,
        "The tool arguments were invalid. Review the schema and fix the arguments.",
    ),
    # JSON / parse errors — usually bad args, but for write_file they are
    # deterministic (same malformed content fails identically on retry).
    # Classify generically; the per-tool refinement below handles write_file.
    (
        re.compile(r"json|parse|decode|unexpected token|syntax error", re.I),
        ToolErrorClass.validation,
        RecoveryAction.fix_args,
        "The input could not be parsed. Check the format of the arguments.",
    ),
]

# ── Per-tool error refinements (#2169, #2168) ───────────────────────────
#
# Override the generic classification for tools where the error class has a
# different recoverability profile.  ``refine_classification`` is called after
# ``classify_tool_error`` to narrow the result when the tool+error combination
# has a more specific recovery action.
_PERMANENT_TOOLS: dict[str, frozenset[ToolErrorClass]] = {
    # #2169 — write_file parse-errors are deterministic: the same malformed
    # JSON/YAML/TOML content will fail validation identically on every retry.
    # Map to permanent/abort so the circuit breaker fires and the hint
    # steers the agent to fix the content or use an alternative.
    "write_file": frozenset({ToolErrorClass.validation}),
}


def refine_classification(
    failure: ToolFailure,
) -> ToolFailure:
    """Narrow a generic classification for tool-specific recoverability (#2169, #2168).

    Called after ``classify_tool_error`` when the caller knows the tool name.
    Returns a possibly-updated ``ToolFailure`` with a more specific error
    class, recovery action, and hint.
    """
    permanent_classes = _PERMANENT_TOOLS.get(failure.tool_name)
    if permanent_classes and failure.error_class in permanent_classes:
        failure.error_class = ToolErrorClass.permanent
        failure.recovery_action = RecoveryAction.abort
        failure.hint = (
            "Content failed validation and the same input will fail identically on retry. "
            "Fix the structural issue (check the exact validation error), or use `terminal` "
            "with a heredoc as an alternative write method. Do NOT blind-retry the same content."
        )
    # #2168 — permission errors: steer toward alternatives instead of just
    # "check credentials", which the agent often can't act on (it can't
    # elevate). Use_alternable/escalate gives the model a fallback chain.
    elif failure.error_class == ToolErrorClass.permission:
        failure.recovery_action = RecoveryAction.use_alternative
        failure.hint = (
            "Access was denied. Try an alternative path, tool, or approach "
            "(e.g. a different directory, `terminal` instead of `write_file`, "
            "or a different API). If no alternative exists, escalate to the "
            "user with the exact access needed. Do NOT retry the same action."
        )
    return failure


def classify_tool_error(
    tool_name: str, error_message: str, attempt: int = 1
) -> ToolFailure:
    """Classify a tool-level error and suggest a recovery action.

    Parameters
    ----------
    tool_name : str
        The name of the tool that failed (e.g. ``"terminal"``, ``"read_file"``).
    error_message : str
        The error message string (from the exception or the ``{"error": ...}`` JSON).
    attempt : int
        The attempt number (1-based). Not currently used for classification
        but available for future circuit-breaker logic.

    Returns
    -------
    ToolFailure
        A classified failure with a recovery hint.
    """
    msg_lower = error_message.lower() if error_message else ""

    result = None
    for pattern, err_class, action, hint in _PATTERNS:
        if pattern.search(msg_lower):
            result = ToolFailure(
                tool_name=tool_name,
                error_message=error_message,
                error_class=err_class,
                recovery_action=action,
                hint=hint,
                attempt_number=attempt,
            )
            break

    if result is None:
        # Default: unknown — can't suggest a specific recovery
        result = ToolFailure(
            tool_name=tool_name,
            error_message=error_message,
            error_class=ToolErrorClass.unknown,
            recovery_action=RecoveryAction.escalate,
            hint="The error could not be classified. Review the error message and decide how to proceed.",
            attempt_number=attempt,
        )

    # #2169, #2168 — narrow the classification for tool-specific recoverability
    return refine_classification(result)


# ── Exception-type-aware classification (#2245) ─────────────────────────
#
# tool_call (deferred-tool / MCP dispatch) failures surface as opaque "other"
# errors because ``classify_tool_error`` only sees ``str(exc)`` — the exception
# *type* (TimeoutError, ConnectionError, KeyError, HTTP-status wrappers) is lost,
# landing in the ``unknown`` bucket with zero recovery guidance (291 "other"
# failures / 7d, max 13-deep spiral). ``classify_tool_exception`` inspects the
# exception object directly before falling back to the string classifier.


def _resolve_timeout_types() -> tuple[type, ...]:
    import asyncio

    types: list[type] = [asyncio.TimeoutError, TimeoutError]
    try:
        from concurrent.futures import TimeoutError as _FT

        types.append(_FT)
    except Exception:
        pass
    return tuple(types)


def _resolve_connection_types() -> tuple[type, ...]:
    types: list[type] = [ConnectionError, OSError]
    try:
        import aiohttp

        types.append(aiohttp.ClientError)
    except Exception:
        pass
    try:
        import httpx

        types.append(httpx.TransportError)
    except Exception:
        pass
    return tuple(types)


_EXC_MAP: list[tuple[tuple[type, ...], ToolErrorClass, RecoveryAction, str]] = [
    (
        _resolve_timeout_types(),
        ToolErrorClass.transient,
        RecoveryAction.retry,
        "The deferred tool call timed out. Retry tool_call — if it times out again, try a lighter-weight alternative.",
    ),
    (
        _resolve_connection_types(),
        ToolErrorClass.transient,
        RecoveryAction.retry,
        "Connection error reaching the tool server (MCP). Retry once; if it persists the server may be down — try an alternative tool.",
    ),
    (
        (ValueError, TypeError),
        ToolErrorClass.validation,
        RecoveryAction.fix_args,
        "The tool rejected the arguments. Call tool_describe to see the schema, then retry with correctly-typed args.",
    ),
    (
        (KeyError,),
        ToolErrorClass.not_found,
        RecoveryAction.use_alternative,
        "The tool server reported a missing key — tool name or registry entry not found. Use tool_search to list available tools.",
    ),
]


def classify_tool_exception(
    tool_name: str, exc: BaseException, attempt: int = 1
) -> ToolFailure:
    """Classify a deferred-tool-call exception by inspecting its *type* (#2245).

    Falls back to ``classify_tool_error`` on the stringified message when the
    exception type is not recognised, ensuring no regression for existing paths.
    """
    # Check HTTP-status-bearing exceptions first (instance attrs, not type).
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if status is not None:
        try:
            status = int(status)
        except (TypeError, ValueError):
            status = None
    if status is not None:
        if status == 404:
            return ToolFailure(
                tool_name,
                str(exc),
                ToolErrorClass.not_found,
                RecoveryAction.use_alternative,
                "The tool returned 404 — not found. Use tool_search to confirm the name.",
                attempt,
            )
        if 500 <= status < 600:
            return ToolFailure(
                tool_name,
                str(exc),
                ToolErrorClass.transient,
                RecoveryAction.use_alternative,
                "Server error (5xx). Retry once, then switch to an alternative tool.",
                attempt,
            )
        if 400 <= status < 500:
            return ToolFailure(
                tool_name,
                str(exc),
                ToolErrorClass.validation,
                RecoveryAction.fix_args,
                "Client error (4xx). Review the arguments and retry.",
                attempt,
            )

    for exc_types, cls, action, hint in _EXC_MAP:
        if isinstance(exc, exc_types):
            return ToolFailure(tool_name, str(exc), cls, action, hint, attempt)

    # Fallback: string-based classification (preserves existing behaviour).
    return classify_tool_error(tool_name, str(exc), attempt)


def recovery_hint(failure: ToolFailure) -> str:
    """Format a recovery hint string suitable for appending to a tool error result.

    Returns a short, actionable suggestion. If the error class is
    ``unknown``, returns an empty string (no hint is better than a
    misleading one).
    """
    if failure.error_class == ToolErrorClass.unknown:
        return ""
    return f" [{failure.recovery_action.value}: {failure.hint}]"


# ── Circuit breaker (lightweight, per-tool) ──────────────────────────────


@dataclass
class CircuitBreaker:
    """Simple per-tool circuit breaker.

    Tracks consecutive failures for a single tool. After ``threshold``
    consecutive failures, the circuit opens and ``should_trip()`` returns
    True — callers can use this to fail-fast instead of retrying.

    The breaker resets on any success. It does not auto-transition to
    half-open; a success call manually resets it.

    This is intentionally minimal — no timeout-based half-open, no rolling
    window. Just consecutive-count → trip. Enough to prevent infinite
    retry loops on a permanently broken tool without adding complexity.
    """

    threshold: int = 5
    _consecutive_failures: int = 0
    _is_open: bool = False

    def record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.threshold:
            self._is_open = True
            logger.warning(
                "circuit breaker opened for tool after %d consecutive failures",
                self._consecutive_failures,
            )

    def record_success(self) -> None:
        self._consecutive_failures = 0
        self._is_open = False

    def should_trip(self) -> bool:
        return self._is_open


# ── Per-tool breaker registry (process-global) ───────────────────────────

_breakers: dict[str, CircuitBreaker] = {}


def get_breaker(tool_name: str, threshold: int = 5) -> CircuitBreaker:
    """Get or create a circuit breaker for a tool name."""
    if tool_name not in _breakers:
        _breakers[tool_name] = CircuitBreaker(threshold=threshold)
    return _breakers[tool_name]


def record_tool_outcome(tool_name: str, success: bool) -> None:
    """Record a tool call outcome for circuit-breaker tracking.

    Called from ``handle_function_call`` after every tool dispatch.
    On failure, increments the consecutive failure count. On success,
    resets the breaker. When the breaker trips, a warning is logged.
    """
    breaker = get_breaker(tool_name)
    if success:
        breaker.record_success()
    else:
        breaker.record_failure()
