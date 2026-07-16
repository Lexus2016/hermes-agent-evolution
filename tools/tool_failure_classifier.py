"""Cross-tool failure classifier (PALADIN-style structured recovery, slice 1).

Generalizes the terminal-only ``terminal_failure_classifier`` into a single
entry point that classifies failures from any core tool — terminal, file,
search, browser, delegate, and others — into structured categories the agent
can act on (switch tool, retry with backoff, fix arguments, or surface a
blocker) instead of looping on the same failing call.

This is the first slice of issue #1019 (PALADIN execution-level tool-failure
recovery): classification only. Root-cause diagnosis (#1026), the recovery
strategy dispatcher (#1027), and wiring into the tool execution loop are
follow-up slices — this module deliberately does not change any tool's
behavior.

Design notes:
- Terminal failures delegate to the existing ``classify_terminal_failure`` so
  its richer exit-code/signal/streak logic is preserved, then the result is
  mapped onto the shared category enum.
- Every other tool is classified from its error text against an ordered rule
  table (first match wins). The table and the tool→family map are both
  extensible at runtime via ``register_rule`` / ``register_tool_family``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re

from tools.terminal_failure_classifier import (
    FailureCategory as _TerminalCategory,
    classify_terminal_failure,
)


class ToolType(str, Enum):
    """Coarse tool family used to tailor classification and hints."""

    terminal = "terminal"
    file = "file"
    search = "search"
    browser = "browser"
    delegate = "delegate"
    generic = "generic"


class ToolFailureCategory(str, Enum):
    """Structured, cross-tool failure categories."""

    tool_unavailable = "tool_unavailable"
    invalid_arguments = "invalid_arguments"
    not_found = "not_found"
    permission_denied = "permission_denied"
    rate_limited = "rate_limited"
    # Covers transient network failures AND local transient resource contention
    # (file/index locks, "device or resource busy"). The recovery dispatcher
    # (#1027) must not assume this always implies remote connectivity.
    transient_network = "transient_network"
    timeout = "timeout"
    unexpected_output = "unexpected_output"
    persistent_error = "persistent_error"
    unknown = "unknown"


@dataclass(frozen=True)
class ToolFailureClassification:
    """Result of classifying a tool failure."""

    category: ToolFailureCategory
    tool_type: ToolType
    hint: str
    should_retry: bool


# ---------------------------------------------------------------------------
# Tool → family mapping (extensible)
# ---------------------------------------------------------------------------

# Substring markers checked against a normalized (lowercased) tool name.
# Ordered most-specific first so, e.g., "web_search" resolves to search rather
# than being mistaken for a generic tool.
_FAMILY_MARKERS: list[tuple[str, ToolType]] = [
    ("terminal", ToolType.terminal),
    ("shell", ToolType.terminal),
    ("bash", ToolType.terminal),
    ("search", ToolType.search),
    ("grep", ToolType.search),
    ("glob", ToolType.search),
    ("browser", ToolType.browser),
    ("computer_use", ToolType.browser),
    ("navigate", ToolType.browser),
    ("delegate", ToolType.delegate),
    ("agent_team", ToolType.delegate),
    ("spawn", ToolType.delegate),
    ("read_file", ToolType.file),
    ("write_file", ToolType.file),
    ("edit_file", ToolType.file),
    ("apply_patch", ToolType.file),
    ("patch", ToolType.file),
    ("file", ToolType.file),
]

# Exact-name overrides registered at runtime take priority over markers.
_FAMILY_OVERRIDES: dict[str, ToolType] = {}


def register_tool_family(tool_name: str, tool_type: ToolType) -> None:
    """Register an exact tool-name → family mapping (highest priority)."""
    _FAMILY_OVERRIDES[tool_name.strip().lower()] = tool_type


def tool_family(tool_name: str) -> ToolType:
    """Resolve a tool name to its coarse family."""
    key = (tool_name or "").strip().lower()
    if key in _FAMILY_OVERRIDES:
        return _FAMILY_OVERRIDES[key]
    for marker, tool_type in _FAMILY_MARKERS:
        if marker in key:
            return tool_type
    return ToolType.generic


# ---------------------------------------------------------------------------
# Text classification rules (extensible, first match wins)
# ---------------------------------------------------------------------------

# Default retry disposition per category: True means a plain retry (with
# backoff) has a reasonable chance of succeeding. Argument/permission/missing
# failures are deterministic and must not be retried unchanged.
_CATEGORY_RETRYABLE: dict[ToolFailureCategory, bool] = {
    ToolFailureCategory.tool_unavailable: False,
    ToolFailureCategory.invalid_arguments: False,
    ToolFailureCategory.not_found: False,
    ToolFailureCategory.permission_denied: False,
    ToolFailureCategory.rate_limited: True,
    ToolFailureCategory.transient_network: True,
    ToolFailureCategory.timeout: True,
    ToolFailureCategory.unexpected_output: True,
    ToolFailureCategory.persistent_error: False,
    ToolFailureCategory.unknown: False,
}

_CATEGORY_HINTS: dict[ToolFailureCategory, str] = {
    ToolFailureCategory.tool_unavailable: (
        "The tool or one of its dependencies is unavailable (not installed, not "
        "connected, or disabled). Do not retry — switch to an alternative tool "
        "or resolve the dependency."
    ),
    ToolFailureCategory.invalid_arguments: (
        "The call arguments are invalid or incomplete. Retrying unchanged will "
        "fail again — correct the arguments (check required fields, allowed "
        "values, and exact match text) before calling again."
    ),
    ToolFailureCategory.not_found: (
        "The target does not exist. Verify the path/identifier (e.g. list the "
        "directory or search first) rather than retrying the same lookup."
    ),
    ToolFailureCategory.permission_denied: (
        "Access was denied. Do not retry — check permissions/credentials, use a "
        "resource you own, or ask the user before escalating."
    ),
    ToolFailureCategory.rate_limited: (
        "A rate limit or quota was hit. Back off and retry after a delay, or "
        "reduce request frequency."
    ),
    ToolFailureCategory.transient_network: (
        "A transient network/resource issue occurred. A retry with backoff may "
        "succeed; if it recurs, switch approach."
    ),
    ToolFailureCategory.timeout: (
        "The operation timed out. Retry with a longer timeout, run it in the "
        "background, or split the work into smaller steps."
    ),
    ToolFailureCategory.unexpected_output: (
        "The tool returned malformed or empty output. Retry once; if it repeats, "
        "adjust the request or switch tools."
    ),
    ToolFailureCategory.persistent_error: (
        "The tool failed with a persistent error. Review the output and fix the "
        "underlying cause or switch tools instead of retrying identically."
    ),
    ToolFailureCategory.unknown: (
        "The failure could not be classified. Inspect the raw error before "
        "deciding whether to retry or change approach."
    ),
}


@dataclass(frozen=True)
class _Rule:
    pattern: re.Pattern[str]
    category: ToolFailureCategory
    should_retry: bool | None  # None -> use the category default


def _rule(
    pattern: str,
    category: ToolFailureCategory,
    should_retry: bool | None = None,
) -> _Rule:
    return _Rule(re.compile(pattern, re.IGNORECASE), category, should_retry)


# Ordered so more specific patterns win over generic ones. Ordering hazards
# worth calling out (first match wins):
#   - argument-shaped "X not found" precede the generic not_found rule;
#   - specific transient/timeout patterns precede the generic ``blocked:``
#     rule so "blocked: connection timed out" is not read as a permission block;
#   - dependency "<component> not available" precedes the generic "not available"
#     -> not_found rule so a missing adapter/plugin is not mistaken for missing
#     data (and vice versa).
_BUILTIN_RULES: list[_Rule] = [
    # Argument-shaped "X not found" (missing required field / edit's old_string)
    # before the generic not_found rule.
    _rule(r"(?:required|argument|parameter)\b.*\bnot found", ToolFailureCategory.invalid_arguments),
    _rule(r"old_(?:string|text)\b.*not found", ToolFailureCategory.invalid_arguments),
    # Rate limits / quota.
    _rule(r"rate[ _-]?limit", ToolFailureCategory.rate_limited),
    _rule(r"\b429\b", ToolFailureCategory.rate_limited),
    _rule(r"too many requests", ToolFailureCategory.rate_limited),
    _rule(r"quota exceeded", ToolFailureCategory.rate_limited),
    # Permission / authorization.
    _rule(r"must be (?:logged in|authenticated|authori[sz]ed)", ToolFailureCategory.permission_denied),
    _rule(r"permission denied", ToolFailureCategory.permission_denied),
    _rule(r"operation not permitted", ToolFailureCategory.permission_denied),
    _rule(r"access denied", ToolFailureCategory.permission_denied),
    _rule(r"unauthorized", ToolFailureCategory.permission_denied),
    _rule(r"forbidden", ToolFailureCategory.permission_denied),
    _rule(r"\b403\b", ToolFailureCategory.permission_denied),
    _rule(r"read-only file system", ToolFailureCategory.permission_denied),
    # Transient network / resource hiccups (before the generic timeout rule so
    # "connection timed out" is treated as a network issue).
    _rule(r"could not resolve host", ToolFailureCategory.transient_network),
    _rule(r"connection refused", ToolFailureCategory.transient_network),
    _rule(r"connection reset", ToolFailureCategory.transient_network),
    _rule(r"network is unreachable", ToolFailureCategory.transient_network),
    _rule(r"no route to host", ToolFailureCategory.transient_network),
    _rule(r"temporarily unavailable", ToolFailureCategory.transient_network),
    _rule(r"connection timed out", ToolFailureCategory.transient_network),
    # Generic timeout.
    _rule(r"timed out", ToolFailureCategory.timeout),
    _rule(r"\btimeout\b", ToolFailureCategory.timeout),
    # Security/policy block (after transient/timeout so a "blocked: <transient>"
    # message is not misread as a hard permission failure).
    _rule(r"^\s*blocked:", ToolFailureCategory.permission_denied),
    # Tool / dependency unavailable (includes the "command not found" text form
    # before the generic not_found rule). "<component> not available" is scoped
    # to dependency-shaped nouns so missing *data* falls through to not_found.
    _rule(r"command not found", ToolFailureCategory.tool_unavailable),
    _rule(r"not recognized as an internal or external command", ToolFailureCategory.tool_unavailable),
    _rule(r"unknown command", ToolFailureCategory.tool_unavailable),
    _rule(r"not connected", ToolFailureCategory.tool_unavailable),
    _rule(r"not installed", ToolFailureCategory.tool_unavailable),
    _rule(
        r"(?:adapter|plugin|service|backend|integration|provider|package|module|api|extension|driver|server|daemon)"
        r"\b[^.]*\bnot available",
        ToolFailureCategory.tool_unavailable,
    ),
    _rule(r"not registered", ToolFailureCategory.tool_unavailable),
    _rule(r"not configured", ToolFailureCategory.tool_unavailable),
    _rule(r"requirements not met", ToolFailureCategory.tool_unavailable),
    _rule(r"is disabled", ToolFailureCategory.tool_unavailable),
    _rule(r"no module named", ToolFailureCategory.tool_unavailable),
    # Malformed / empty output.
    _rule(r"non-dict result", ToolFailureCategory.unexpected_output),
    _rule(r"non-json output", ToolFailureCategory.unexpected_output),
    _rule(r"returned no output", ToolFailureCategory.unexpected_output),
    _rule(r"returned empty", ToolFailureCategory.unexpected_output),
    _rule(r"empty transcript", ToolFailureCategory.unexpected_output),
    _rule(r"returned none", ToolFailureCategory.unexpected_output),
    _rule(r"malformed", ToolFailureCategory.unexpected_output),
    _rule(r"invalid json", ToolFailureCategory.unexpected_output),
    # Missing target (generic "not available" meaning missing data lands here).
    _rule(r"does not exist", ToolFailureCategory.not_found),
    _rule(r"no such file", ToolFailureCategory.not_found),
    _rule(r"no such", ToolFailureCategory.not_found),
    _rule(r"\bno \w+ exists?\b", ToolFailureCategory.not_found),
    _rule(r"not available", ToolFailureCategory.not_found),
    _rule(r"not found", ToolFailureCategory.not_found),
    # Generic invalid arguments.
    _rule(r"\brequired\b", ToolFailureCategory.invalid_arguments),
    _rule(r"cannot be empty", ToolFailureCategory.invalid_arguments),
    _rule(r"must be\b", ToolFailureCategory.invalid_arguments),
    _rule(r"unknown mode", ToolFailureCategory.invalid_arguments),
    _rule(r"provide either", ToolFailureCategory.invalid_arguments),
    _rule(r"(?:is )?missing a\b", ToolFailureCategory.invalid_arguments),
    _rule(r"invalid (?:argument|parameter|mode|value)", ToolFailureCategory.invalid_arguments),
]

# Runtime-registered rules take priority over the built-ins.
_CUSTOM_RULES: list[_Rule] = []


def register_rule(
    pattern: str,
    category: ToolFailureCategory,
    should_retry: bool | None = None,
) -> None:
    """Register a custom classification rule (checked before the built-ins)."""
    _CUSTOM_RULES.append(_rule(pattern, category, should_retry))


# Terminal category → shared category. Terminal keeps its own richer logic; we
# only remap the label so callers see one vocabulary.
_TERMINAL_CATEGORY_MAP: dict[_TerminalCategory, ToolFailureCategory] = {
    _TerminalCategory.retryable_transient: ToolFailureCategory.transient_network,
    _TerminalCategory.missing_command: ToolFailureCategory.tool_unavailable,
    _TerminalCategory.permission_denied: ToolFailureCategory.permission_denied,
    _TerminalCategory.persistent_error: ToolFailureCategory.persistent_error,
    # Deterministic timeout (repeated identical failures) maps to persistent
    # — the call cannot succeed unchanged (issue #1091).
    _TerminalCategory.timeout_deterministic: ToolFailureCategory.persistent_error,
    _TerminalCategory.timeout: ToolFailureCategory.timeout,
    _TerminalCategory.unknown: ToolFailureCategory.unknown,
}


def _retry_for(category: ToolFailureCategory, override: bool | None) -> bool:
    return _CATEGORY_RETRYABLE[category] if override is None else override


def _classify_text(text: str, tool_type: ToolType) -> ToolFailureClassification:
    for rule in (*_CUSTOM_RULES, *_BUILTIN_RULES):
        if rule.pattern.search(text):
            return ToolFailureClassification(
                category=rule.category,
                tool_type=tool_type,
                hint=_CATEGORY_HINTS[rule.category],
                should_retry=_retry_for(rule.category, rule.should_retry),
            )
    category = (
        ToolFailureCategory.unknown
        if not text.strip()
        else ToolFailureCategory.persistent_error
    )
    return ToolFailureClassification(
        category=category,
        tool_type=tool_type,
        hint=_CATEGORY_HINTS[category],
        should_retry=_CATEGORY_RETRYABLE[category],
    )


def classify_tool_failure(
    tool_name: str,
    error: str = "",
    *,
    exit_code: int | None = None,
    consecutive_count: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> ToolFailureClassification:
    """Classify a failure from any core tool.

    Args:
        tool_name: Name of the tool that failed (used to resolve its family).
        error: The tool's error text (for terminal, treated as stderr if
            ``stderr`` is not given).
        exit_code: Process exit code, when the tool has one. Terminal tools use
            this for their richer exit-code/signal logic.
        consecutive_count: Consecutive failures without progress; high values
            downgrade retryable terminal cases to persistent.
        stdout: Standard output, when available.
        stderr: Standard error, when available.

    Returns:
        A ToolFailureClassification with category, tool family, actionable hint,
        and whether a plain retry is worthwhile.
    """
    tool_type = tool_family(tool_name)

    if tool_type is ToolType.terminal and exit_code is not None:
        terminal = classify_terminal_failure(
            command="",
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr or error,
            consecutive_count=consecutive_count,
        )
        return ToolFailureClassification(
            category=_TERMINAL_CATEGORY_MAP[terminal.category],
            tool_type=ToolType.terminal,
            hint=terminal.hint,
            should_retry=terminal.should_retry,
        )

    text = "\n".join(part for part in (error, stdout, stderr) if part)
    return _classify_text(text, tool_type)
