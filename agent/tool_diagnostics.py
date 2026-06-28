"""Normalize tool failures into an actionable diagnostic hint (#130, #175).

Raw tool failures arrive in many shapes (non-zero exits, permission errors,
timeouts, not-found, char-limit caps). The model then has to interpret each one
and often just retries the same call. This classifies a failing tool result into
a small, stable TAXONOMY and returns a one-line recovery hint that
``make_tool_result_message`` appends to the result the model sees.

Pure + deterministic. Proactive, per-failure complement to ``loop_guard`` (which
reacts only after a failure REPEATS). Categories deliberately map to the
recovery routes the cluster issues asked for: limit / permission / not_found /
timeout / missing_command / runtime_error.
"""

from __future__ import annotations

import re
from typing import Any, Optional, Tuple

# Ordered most-specific first; first match wins. (regex, category, hint).
_RULES: tuple[tuple[re.Pattern, str, str], ...] = (
    (
        re.compile(
            r"command not found|not recognized as|: No such file or directory.*\b(sh|bash|exec)\b|executable file not found",
            re.I,
        ),
        "missing_command",
        "A required binary/command is missing. Check prerequisites first "
        "(`which <cmd>` / install it), or use a different tool — do NOT repeat the same command.",
    ),
    (
        re.compile(
            r"permission denied|access denied|not permitted|forbidden|refusing to write|operation not permitted|EACCES",
            re.I,
        ),
        "permission",
        "Access is denied and the agent can't elevate. Do NOT retry the same path — "
        "use an allowed path/tool, or report exactly what access is needed.",
    ),
    (
        re.compile(
            r"timed out|timeout|deadline exceeded|ClosedResourceError|unreachable|connection refused|ETIMEDOUT",
            re.I,
        ),
        "timeout",
        "The operation timed out / the resource is unreachable. Set it aside, route to a "
        "fallback if one exists, and do NOT loop on health checks or retry blindly.",
    ),
    (
        re.compile(
            r"\b(char(acter)?s?|byte)s?\b.*\b(limit|exceed|too (long|large|big)|maximum)\b|exceeds the maximum|max(imum)? (length|size)|too many tokens|context length",
            re.I,
        ),
        "limit",
        "A size/length limit was hit. Don't resend as-is — chunk the work, summarize, "
        "or raise the relevant config limit; a near-identical retry will fail the same way.",
    ),
    (
        re.compile(
            r"\bno results\b|\bno results found\b|duckduckgo search failed|brave search returned http|could not reach .* search|searxng returned http",
            re.I,
        ),
        "provider_dead",
        "The active search provider is not returning results. Switch to a different "
        "search_backend in config.yaml or via hermes tools, or report the blocker "
        "if no alternative provider is configured. Do not retry the same query with "
        "the same provider.",
    ),
    (
        re.compile(
            r"no such file or directory|not found|does not exist|cannot find|no matches found|0 results|no results",
            re.I,
        ),
        "not_found",
        "The target wasn't found. Re-check the path/name (it may be dynamic), broaden the "
        "search, or create the prerequisite first — don't repeat the same lookup.",
    ),
    (
        re.compile(
            r"traceback \(most recent call|exit code[:\s]+[1-9]|exit status [1-9]|non-zero exit|error:|exception|failed",
            re.I,
        ),
        "runtime_error",
        "The call errored. Read the message, fix the root cause, and CHANGE the call — "
        "retrying the same arguments will reproduce it.",
    ),
)


def classify(content: Any) -> Optional[Tuple[str, str]]:
    """Return (category, recovery_hint) if the content looks like a failure, else None."""
    if not isinstance(content, str) or not content.strip():
        return None
    for pattern, category, hint in _RULES:
        if pattern.search(content):
            return category, hint
    return None


def diagnostic_suffix(content: Any) -> str:
    """Return a one-line diagnostic annotation to append to a failing tool
    result, or '' if the result is not a recognized failure. Trusted text
    (our annotation), kept outside any untrusted-content wrapper by the caller."""
    hit = classify(content)
    if not hit:
        return ""
    category, hint = hit
    return f"\n\n[diagnostic] failure-class={category} — {hint}"
