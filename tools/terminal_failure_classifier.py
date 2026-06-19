"""Lightweight failure classifier for the terminal tool.

Provides structured categories for non-zero terminal exits and execution
errors so the agent can decide whether to retry, switch tools, or surface a
blocker to the user instead of looping on the same failing command.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re


class FailureCategory(str, Enum):
    """Known terminal failure categories."""

    retryable_transient = "retryable_transient"
    missing_command = "missing_command"
    permission_denied = "permission_denied"
    persistent_error = "persistent_error"
    timeout = "timeout"
    unknown = "unknown"


@dataclass(frozen=True)
class TerminalFailureClassification:
    """Result of classifying a terminal failure."""

    category: FailureCategory
    hint: str
    should_retry: bool


_MISSING_CMD_PATTERNS = (
    re.compile(r"command not found", re.IGNORECASE),
    re.compile(r"not found", re.IGNORECASE),
    re.compile(r"unknown command", re.IGNORECASE),
    re.compile(r"is not recognized as an internal or external command", re.IGNORECASE),
    re.compile(r"No such file or directory", re.IGNORECASE),
    re.compile(r"could not find command", re.IGNORECASE),
)

_PERMISSION_DENIED_PATTERNS = (
    re.compile(r"permission denied", re.IGNORECASE),
    re.compile(r"operation not permitted", re.IGNORECASE),
    re.compile(r"access denied", re.IGNORECASE),
    re.compile(r"read-only file system", re.IGNORECASE),
    re.compile(r"cannot open.*permission", re.IGNORECASE),
)

_TIMEOUT_PATTERNS = (
    re.compile(r"timed out", re.IGNORECASE),
    re.compile(r"timeout", re.IGNORECASE),
    re.compile(r"operation timed out", re.IGNORECASE),
    re.compile(r"connection timed out", re.IGNORECASE),
)

_TRANSIENT_PATTERNS = (
    re.compile(r"resource temporarily unavailable", re.IGNORECASE),
    re.compile(r"try again", re.IGNORECASE),
    re.compile(r"temporary failure", re.IGNORECASE),
    re.compile(r"network is unreachable", re.IGNORECASE),
    re.compile(r"connection refused", re.IGNORECASE),
    re.compile(r"could not resolve host", re.IGNORECASE),
    re.compile(r"device or resource busy", re.IGNORECASE),
)

# Commands where a non-zero exit code is often informational rather than an
# error.  These should not be classified as persistent failures.
_EXPECTED_NONZERO_COMMANDS = frozenset({
    "grep",
    "egrep",
    "fgrep",
    "rg",
    "ag",
    "ack",
    "diff",
    "colordiff",
    "find",
    "test",
    "[",
    "git",
})


def _base_command(command: str) -> str:
    """Extract the base command name from a shell pipeline/chain."""
    # Split on shell operators and take the last segment because the exit code
    # in a chain is determined by the final command.
    segments = re.split(r"\s*(?:\|\||&&|[|;])\s*", command)
    last_segment = (segments[-1] if segments else command).strip()
    words = last_segment.split()
    for w in words:
        if "=" in w and not w.startswith("-"):
            continue  # skip VAR=val
        return w.split("/")[-1]
    return ""


def _output_text(stdout: str, stderr: str) -> str:
    """Return a single string suitable for pattern matching."""
    parts = []
    for part in (stdout, stderr):
        if part:
            parts.append(part)
    return "\n".join(parts)


def classify_terminal_failure(
    command: str,
    exit_code: int,
    stdout: str,
    stderr: str,
    consecutive_count: int = 0,
) -> TerminalFailureClassification:
    """Classify a terminal failure and return a user-facing recommendation.

    Args:
        command: The command that failed.
        exit_code: The exit code from the command (or -1 for internal errors).
        stdout: Command standard output.
        stderr: Command standard error.
        consecutive_count: Number of consecutive terminal invocations without
            observable state change.  High values downgrade retryable cases to
            persistent so the agent is forced to switch strategy.

    Returns:
        TerminalFailureClassification with category, human-readable hint, and
        whether the caller should retry the same command.
    """
    text = _output_text(stdout, stderr)
    base_cmd = _base_command(command)

    # Transient timeout / partial output is safe to retry with backoff.
    if exit_code == 124 or any(p.search(text) for p in _TIMEOUT_PATTERNS):
        if consecutive_count >= 3:
            return TerminalFailureClassification(
                category=FailureCategory.persistent_error,
                hint=(
                    "The command has timed out repeatedly ("
                    f"{consecutive_count} consecutive terminal calls). "
                    "Consider running it as a background process with "
                    "notify_on_complete=true, or break the work into smaller steps."
                ),
                should_retry=False,
            )
        return TerminalFailureClassification(
            category=FailureCategory.timeout,
            hint=(
                "The command timed out. Retry with a longer timeout, run it in "
                "the background with notify_on_complete=true, or split the work."
            ),
            should_retry=True,
        )

    # Missing command / binary not on PATH.
    if exit_code == 127 or any(p.search(text) for p in _MISSING_CMD_PATTERNS):
        return TerminalFailureClassification(
            category=FailureCategory.missing_command,
            hint=(
                "The command was not found. Verify the binary is installed, "
                "use the full path, or switch to a different tool (read_file, "
                "execute_code, web_search)."
            ),
            should_retry=False,
        )

    # Permission denied / insufficient privileges.
    if exit_code == 126 or any(p.search(text) for p in _PERMISSION_DENIED_PATTERNS):
        return TerminalFailureClassification(
            category=FailureCategory.permission_denied,
            hint=(
                "Permission denied. Check file permissions, run from a "
                "directory you own, or ask the user before escalating privileges."
            ),
            should_retry=False,
        )

    # Some commands use non-zero exit codes for normal informational purposes.
    if base_cmd in _EXPECTED_NONZERO_COMMANDS:
        return TerminalFailureClassification(
            category=FailureCategory.unknown,
            hint=(
                f"{base_cmd} returned exit code {exit_code}, which may be "
                "informational rather than an error."
            ),
            should_retry=False,
        )

    # Retryable transient errors (network/resource hiccups).
    if any(p.search(text) for p in _TRANSIENT_PATTERNS):
        if consecutive_count >= 3:
            return TerminalFailureClassification(
                category=FailureCategory.persistent_error,
                hint=(
                    f"The same transient error has occurred {consecutive_count} "
                    "times in a row. Stop retrying and either switch tools or "
                    "ask the user."
                ),
                should_retry=False,
            )
        return TerminalFailureClassification(
            category=FailureCategory.retryable_transient,
            hint=(
                "A transient resource or network issue occurred. A retry with "
                "backoff may succeed."
            ),
            should_retry=True,
        )

    # Default: persistent non-zero error.
    return TerminalFailureClassification(
        category=FailureCategory.persistent_error,
        hint=(
            "The command failed with a persistent error. Review the output, "
            "fix the underlying issue, or switch to read_file/execute_code/"
            "process/web tools instead of retrying the same command."
        ),
        should_retry=False,
    )


def streak_recommendation(streak: int) -> str | None:
    """Return a recommendation when the terminal streak is high."""
    if streak >= 3:
        return (
            f"Terminal has been invoked {streak} times without state change. "
            "Consider using read_file, execute_code, process, or web tools, "
            "or ask the user before continuing."
        )
    return None
