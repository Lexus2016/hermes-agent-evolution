"""Detect failed-attempt spans in conversation messages.

A *failed attempt* is a tool call whose result indicates an error —
an exception traceback, a non-zero exit code, a file-not-found, or an
explicit ``"error"`` field.  These spans pollute subsequent reasoning
(the model often re-reads the error and retries the same approach).
Marking them lets ``ContextCompressor._prune_old_tool_results``
prioritise their removal during compaction, reducing *contextual drag*
from failed branches (#1580).
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

# ---------------------------------------------------------------------------
# Error-signal patterns
# ---------------------------------------------------------------------------

# Tracebacks / Python exceptions
_TRACEBACK_RE = re.compile(r"Traceback \(most recent call last\)", re.IGNORECASE)
_EXCEPTION_RE = re.compile(r"\b([A-Z]\w*Error|[A-Z]\w*Exception): ", re.MULTILINE)

# Shell / terminal non-zero exit
_EXIT_CODE_RE = re.compile(r"exit[_ ]code[\"']?\s*[:=]\s*([1-9]\d*)", re.IGNORECASE)
_PROCESS_EXIT_RE = re.compile(
    r"\bexit(?:ed)?(?:\s+code)?\s+(?:with\s+)?(?:code\s+)?([1-9]\d*)", re.IGNORECASE
)

# Common error phrases in tool results (JSON {"error": ...}, plain text, etc.)
_ERROR_KEY_RE = re.compile(r'"error"\s*:\s*"', re.IGNORECASE)
_ERROR_LABEL_RE = re.compile(
    r"^\s*(?:error|exception|failed|failure)\b", re.IGNORECASE | re.MULTILINE
)

# Explicit status fields
_STATUS_ERROR_RE = re.compile(r'"status"\s*:\s*"(?:error|failed)"', re.IGNORECASE)

# Loop-guard / retry-exhausted messages
_LOOP_GUARD_RE = re.compile(r"\[loop-guard\]", re.IGNORECASE)

_COMPILED_PATTERNS = (
    _TRACEBACK_RE,
    _EXCEPTION_RE,
    _EXIT_CODE_RE,
    _PROCESS_EXIT_RE,
    _ERROR_KEY_RE,
    _ERROR_LABEL_RE,
    _STATUS_ERROR_RE,
    _LOOP_GUARD_RE,
)


def _is_failed_content(content: Any) -> bool:
    """Return True if *content* (str / list / dict) signals a failure."""
    if isinstance(content, str):
        text = content
    elif isinstance(content, dict):
        # Multimodal / structured tool result
        if content.get("error"):
            return True
        if content.get("status") in ("error", "failed"):
            return True
        text = content.get("text_summary") or content.get("content") or ""
        if not isinstance(text, str):
            text = str(text)
    elif isinstance(content, list):
        # Multimodal parts — check text parts
        text = " ".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    else:
        return False

    if not text:
        return False

    # Quick length gate — very short results rarely carry useful error signals
    # and matching them produces false positives (e.g. the word "Error" in a
    # 20-char completion).
    if len(text) < 30:
        return False

    return any(p.search(text) for p in _COMPILED_PATTERNS)


def failed_attempt_indices(messages: List[Dict[str, Any]]) -> List[int]:
    """Return indices of tool-result messages that are failed attempts.

    Parameters
    ----------
    messages
        The conversation message list (same format the compressor sees).

    Returns
    -------
    list[int]
        Sorted ascending list of indices whose ``role == "tool"`` and whose
        content matches at least one error signal.
    """
    if not messages:
        return []

    indices: List[int] = []
    for i, msg in enumerate(messages):
        if not isinstance(msg, dict):
            continue
        if msg.get("role") != "tool":
            continue
        if _is_failed_content(msg.get("content")):
            indices.append(i)
    return indices


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

__all__ = ["failed_attempt_indices"]
