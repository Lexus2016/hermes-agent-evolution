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

import os
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
    # #1842 — patch-ambiguous: old_string matches multiple locations.
    # The same old_string will match the same locations on retry, so
    # this is deterministic — the agent must add context, not blind-retry.
    # Must run BEFORE permission: the write_file parse-error message
    # contains "Refusing to write" which would otherwise match permission.
    (
        re.compile(
            r"\b(found \d+ matches|multiple matches found)\b",
            re.I,
        ),
        "ambiguous",
        "old_string matched multiple locations. Add more surrounding context to make it "
        "unique, or use replace_all=true — do NOT retry the same old_string.",
    ),
    (
        # #1840 — write_file/patch parse-errors: malformed content (JSON,
        # YAML, TOML) fails syntax validation deterministically. The same
        # malformed content will fail identically on every retry.
        # Must run BEFORE permission: "Refusing to write" in the error
        # message would otherwise match the permission regex.
        re.compile(
            r"\b(jsondecodeerror|yamlerror|tomldecodeerror|parseerror|syntaxerror|"
            r"invalid (json|yaml|toml)|syntax validation|fails? \.(json|yaml|yml|toml) syntax)\b",
            re.I,
        ),
        "parse_error",
        "Content failed syntax validation. The malformed content will fail identically on "
        "retry — fix the syntax error or re-read the source, do NOT blind-retry the same content.",
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
        # Only genuine backend-unreachable signals belong here. A bare
        # "no results" used to match, which mislabelled every empty local
        # search_files result as a dead web-search provider (#1611) — the two
        # need opposite recoveries: an empty local search should be retried
        # with a broader query, a dead provider should not be retried at all.
        # Bare "no results" now falls through to the not_found rule below.
        re.compile(
            r"duckduckgo search failed|brave search returned http|could not reach .* search|searxng returned http",
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


# ---------------------------------------------------------------------------
# Payload anomaly detection (#1495 — Unfaithful Safety Refusal / USR)
#
# When a tool returns a broken payload (None, empty, all-null fields), the
# model gets no explicit failure signal and may fabricate a safety/privacy
# rationale that was never in the system prompt.  ``payload_anomaly`` checks
# the payload *structurally* (not by error-string regex).  When an anomaly is
# found, ``make_tool_result_message`` injects a ``[tool_error]`` signal so the
# model knows the tool malfunctioned — eliminating the ambiguity that triggers
# USR.
# ---------------------------------------------------------------------------

_NULL_TOKENS = frozenset({"none", "null", "nil", "{}", "[]", "error: none"})

_USR_HINT_EMPTY = (
    "The tool returned an empty/null payload — it likely malfunctioned "
    "(not a safety/permission issue). Retry the call, try a fallback tool, "
    "or report the malfunction. Do NOT fabricate a safety rationale."
)
_USR_HINT_LIST = (
    "The tool returned an empty list — it may have found nothing or may "
    "have malfunctioned. Broaden the query or try a different approach. "
    "Do NOT assume a safety restriction is the cause."
)
_USR_HINT_MALFORMED = (
    "The tool returned a structurally empty result (all fields null/empty) — "
    "it likely malfunctioned. Retry the call or try a fallback tool. "
    "Do NOT fabricate a safety rationale."
)


def payload_anomaly(content: Any) -> Optional[Tuple[str, str]]:
    """Classify a structurally broken tool payload (#1495).

    Returns ``(anomaly_type, recovery_hint)`` or ``None`` if structurally valid.
    """
    if content is None:
        return "empty_payload", _USR_HINT_EMPTY
    if isinstance(content, str):
        s = content.strip()
        if not s:
            return "empty_payload", _USR_HINT_EMPTY
        if s.lower() in _NULL_TOKENS:
            return "empty_payload", _USR_HINT_EMPTY
    if isinstance(content, list) and len(content) == 0:
        return "empty_payload", _USR_HINT_LIST
    if isinstance(content, dict):
        if len(content) == 0:
            return "empty_payload", _USR_HINT_EMPTY
        if all(
            v is None
            or (isinstance(v, str) and not v.strip())
            or (isinstance(v, (list, dict)) and len(v) == 0)
            for v in content.values()
        ):
            return "malformed_payload", _USR_HINT_MALFORMED
    return None


def hint_for(category: str) -> Optional[str]:
    """Recovery hint for a known failure category, else None.

    Lets consumers that only stored the category (e.g. loop_guard's run
    tracking) surface the same actionable advice ``classify`` would have
    returned, without re-classifying the original content (#365)."""
    for _pattern, cat, hint in _RULES:
        if cat == category:
            return hint
    return None


def inline_diagnostics_enabled(config: Optional[dict] = None) -> bool:
    """Return whether ``diagnostic_suffix`` may inject its hint into the
    tool result text the model sees.

    Precedence: an explicit ``HERMES_DIAGNOSTICS_INLINE`` env var wins, then
    ``agent.diagnostics.inline`` in config. Default is ``False`` (#606):
    ``classify()`` is a plain regex heuristic over result text, not a real
    success/failure signal, so it repeatedly misfires on successful results
    that merely mention words like "timeout" or "error" — e.g. reading
    source code that discusses them — inflating context with non-actionable
    noise (209 occurrences across 95 sessions in one week of real usage).
    ``loop_guard`` calls ``classify()`` directly for its own tracking and is
    unaffected by this flag either way.
    """
    env = os.environ.get("HERMES_DIAGNOSTICS_INLINE")
    if env is not None:
        return env.strip().lower() not in {"0", "false", "no", "off"}
    if config is None:
        try:
            # Read-only, cache-hit fast path (~130us, no deepcopy) — this
            # runs on every tool result, a hot loop in long sessions.
            from hermes_cli.config import load_config_readonly

            config = load_config_readonly()
        except Exception:
            config = {}
    try:
        from hermes_cli.config import cfg_get

        return bool(cfg_get(config, "agent", "diagnostics", "inline", default=False))
    except Exception:
        return False


def diagnostic_suffix(content: Any, config: Optional[dict] = None) -> str:
    """Return a one-line diagnostic annotation to append to a failing tool
    result, or '' if the result is not a recognized failure or inline
    diagnostics are disabled (the default — see ``inline_diagnostics_enabled``).
    Trusted text (our annotation), kept outside any untrusted-content wrapper
    by the caller."""
    if not inline_diagnostics_enabled(config):
        return ""
    hit = classify(content)
    if not hit:
        return ""
    category, hint = hit
    return f"\n\n[diagnostic] failure-class={category} — {hint}"
