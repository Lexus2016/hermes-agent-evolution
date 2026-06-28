"""Fallback + failure-taxonomy helpers for ``browser_navigate`` (#234/#213).

When navigation fails the agent used to retry ``browser_navigate`` up to 15× in
a row with no recovery. This module gives the failure a small, stable TAXONOMY
and an active fallback to ``web_extract`` (text retrieval of the same URL), plus
a per-URL retry cap so a broken page/backend can't drive an unbounded spiral.

Kept in its own module to avoid a top-level import cycle between
``tools.browser_tool`` and ``tools.web_tools`` — ``browser_tool`` imports it
lazily inside the failure branch, so the heavy web-tools chain only loads once
navigation has already failed.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# Per-URL consecutive navigation-failure counts. After ``MAX_NAV_FAILURES`` the
# fallback layer stops re-attempting the browser for that URL and tells the agent
# to use the extracted text / web_search / report instead of looping. Reset on a
# successful navigation. Process-local; bounded by URL cardinality in a session.
_nav_failures: dict[str, int] = {}
MAX_NAV_FAILURES = 3

# Ordered most-specific first; first match wins. (regex, taxonomy_class).
_NAV_ERROR_RULES: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"tool does not exist|available tools:|unknown ref|no such tool", re.I),
     "tool_not_present"),
    (re.compile(r"\bCDP\b|chrome devtools|websocketdebugger|could not connect to (chrome|browser)|browser (backend|session) (unavailable|not)", re.I),
     "cdp_unavailable"),
    (re.compile(r"timed out|timeout|deadline exceeded|navigation timeout|Page\.navigate", re.I),
     "navigation_timeout"),
    (re.compile(r"could not compute box model|detached|stale element|DOM|selector|element not found", re.I),
     "dom_error"),
)

_TAXONOMY_HINT = {
    "tool_not_present": "The browser backend exposed a different tool set. Do NOT repeat the same "
                        "call — use the extracted text below, web_search, or report the gap.",
    "cdp_unavailable": "The browser backend (CDP/Chrome) is unavailable. Retrying navigation will "
                       "keep failing — use the extracted text below or web_search for this URL.",
    "navigation_timeout": "Navigation timed out. This is deterministic for a slow/blocked page — "
                          "use the extracted text below or web_search instead of re-navigating.",
    "dom_error": "The page DOM could not be read. Use the extracted text below, or fetch via "
                 "web_extract/web_search rather than re-driving the browser.",
    "navigation_error": "Navigation failed. Use the extracted text below or web_search for this URL "
                        "rather than repeating browser_navigate.",
}


def classify_navigation_error(error: Optional[str]) -> str:
    """Map a navigation error string to a stable taxonomy class."""
    if not isinstance(error, str) or not error.strip():
        return "navigation_error"
    for pattern, klass in _NAV_ERROR_RULES:
        if pattern.search(error):
            return klass
    return "navigation_error"


def record_nav_failure(url: str) -> int:
    """Increment and return the consecutive failure count for ``url``."""
    _nav_failures[url] = _nav_failures.get(url, 0) + 1
    return _nav_failures[url]


def reset_nav_failures(url: str) -> None:
    """Clear the failure count for ``url`` after a successful navigation."""
    _nav_failures.pop(url, None)


def web_extract_fallback(url: str) -> Tuple[Optional[str], Optional[str]]:
    """Attempt ``web_extract`` for the URL. Returns (content, error).

    Runs the async web_extract tool in a fresh event loop. If a loop is already
    running in this thread (unexpected for the sync tool-dispatch path), reports
    that rather than crashing the navigation result.
    """
    import asyncio

    try:
        from tools.web_tools import web_extract_tool
    except Exception as exc:  # pragma: no cover - import path/env dependent
        return None, f"web_extract unavailable: {exc}"

    try:
        try:
            asyncio.get_running_loop()
            return None, "web_extract fallback skipped: event loop already running"
        except RuntimeError:
            pass  # no running loop — safe to use asyncio.run
        extracted = asyncio.run(
            web_extract_tool([url], format="markdown", use_llm_processing=False)
        )
        parsed = json.loads(extracted)
    except Exception as exc:
        return None, f"web_extract fallback errored: {exc}"

    if parsed.get("success") is False:
        return None, parsed.get("error", "web_extract returned an error")
    results = parsed.get("results", [])
    if results and results[0].get("content"):
        return results[0]["content"], None
    if results:
        return None, results[0].get("error") or "web_extract returned empty content"
    return None, "web_extract returned no results"


def build_navigation_failure(url: str, error: Optional[str]) -> dict:
    """Build the structured browser_navigate failure result (#234/#213):
    classify, count toward the per-URL cap, attempt the web_extract fallback,
    and attach an actionable recovery directive."""
    klass = classify_navigation_error(error)
    failures = record_nav_failure(url)
    capped = failures >= MAX_NAV_FAILURES

    content, fb_error = web_extract_fallback(url)

    response: dict = {
        "success": False,
        "error": error or "Navigation failed",
        "failure_class": klass,
        "nav_failures_for_url": failures,
    }
    if content:
        response["fallback_used"] = "web_extract"
        response["fallback_content"] = content
        response["recovery"] = (
            "browser_navigate failed but web_extract retrieved the page text "
            "(in fallback_content). Use it directly; do NOT re-navigate."
        )
    else:
        response["fallback_used"] = None
        response["fallback_error"] = fb_error
        hint = _TAXONOMY_HINT.get(klass, _TAXONOMY_HINT["navigation_error"])
        if capped:
            hint = (
                f"Reached the {MAX_NAV_FAILURES}-attempt cap for this URL. STOP "
                f"navigating here. " + hint
            )
        response["recovery"] = hint
    return response
