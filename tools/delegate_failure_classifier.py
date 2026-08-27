"""Typed failure classification for delegate_task child dispatches (#3223 slice 1).

This module mirrors the cross-tool ``tools/tool_failure_classifier.py`` but
specialises on the *dispatch* layer: when a ``delegate_task`` child fails
before or during execution, the parent receives a structured
``failure_class`` drawn from a small, action-oriented vocabulary:

- ``capability-blocked`` — child rejected pre-execution on environment /
  capability grounds (missing tool, blocked tool, permission denied,
  unconfigured dependency).
- ``provider-error`` — upstream provider / API failure (rate limit, quota,
  transient provider/network fault).
- ``timeout`` — child exceeded its execution budget.

The classifier is intentionally conservative.  A failure that does not match any
class stays unclassified; successful dispatches are left untouched.
"""

from __future__ import annotations

import json
import re
from enum import Enum
from typing import Any, Dict, Optional


class DelegateFailureClass(str, Enum):
    """Structured failure classes for delegate_task dispatches."""

    capability_blocked = "capability-blocked"
    provider_error = "provider-error"
    timeout = "timeout"


# Ordered: more specific signals win over generic ones.
_CAPABILITY_MARKERS = [
    r"\bcapability\b",
    r"\bblocked\b",
    r"\bpermission denied\b",
    r"\baccess denied\b",
    r"\bforbidden\b",
    r"\bnot available\b",
    r"\bnot installed\b",
    r"\bnot configured\b",
    r"\bnot registered\b",
    r"\bunknown command\b",
    r"\bcommand not found\b",
    r"\bno module named\b",
    r"\btool_unavailable\b",
]
_PROVIDER_MARKERS = [
    r"\bprovider\b",
    r"\bapi error\b",
    r"\brat(?:e[ _-])?limit\b",
    r"\bquota\b",
    r"\bbilling\b",
    r"\btransient network\b",
    r"\bconnection refused\b",
    r"\bconnection reset\b",
    r"\b429\b",
    r"\b50[0-3]\b",
    r"\b503\b",
]
_TIMEOUT_MARKERS = [
    r"\btimed out\b",
    r"\btimeout\b",
]


def _extract_error_text(data: Dict[str, Any]) -> str:
    """Best-effort extraction of an error string from a delegate result payload."""
    if isinstance(data, dict):
        return str(data.get("error") or data.get("message") or "")
    return ""


def classify_delegate_failure(
    result: Optional[str | Dict[str, Any]] = None,
    *,
    error_text: Optional[str] = None,
) -> Optional[DelegateFailureClass]:
    """Classify a failed delegate_task dispatch.

    Accepts either the raw JSON result, a parsed payload, or an explicit error
    text.  Returns ``None`` for unrecognised or empty input.
    """
    text = ""
    if error_text is not None:
        text = str(error_text)
    elif isinstance(result, str):
        try:
            text = _extract_error_text(json.loads(result))
        except Exception:
            text = result
    elif isinstance(result, dict):
        text = _extract_error_text(result)

    text = text.lower().strip()
    if not text:
        return None

    if any(re.search(p, text) for p in _TIMEOUT_MARKERS):
        return DelegateFailureClass.timeout
    if any(re.search(p, text) for p in _CAPABILITY_MARKERS):
        return DelegateFailureClass.capability_blocked
    if any(re.search(p, text) for p in _PROVIDER_MARKERS):
        return DelegateFailureClass.provider_error
    return None


def inject_delegate_failure_class(payload: Dict[str, Any]) -> bool:
    """Mutate a parsed delegate_task result payload in place.

    Adds ``failure_class`` to the top-level result for tool_error-style
    failures and to each per-task ``results`` entry whose status is not
    ``completed``.  Returns ``True`` if any classification was added.
    """
    changed = False
    top_error = payload.get("error")
    if top_error:
        cls = classify_delegate_failure(error_text=top_error)
        if cls is not None:
            payload["failure_class"] = cls.value
            changed = True

    for entry in payload.get("results") or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("status") == "completed":
            continue
        err = entry.get("error")
        if err:
            cls = classify_delegate_failure(error_text=err)
            if cls is not None:
                entry["failure_class"] = cls.value
                changed = True
    return changed
