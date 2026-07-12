"""Fallback + failure-taxonomy helpers for browser tools (#234/#213/#745).

When navigation fails the agent used to retry ``browser_navigate`` up to 15× in
a row with no recovery. This module gives the failure an active fallback to
``web_extract`` (text retrieval of the same URL) plus a per-URL retry cap so a
broken page/backend can't drive an unbounded spiral.

Failure classification reuses the SHARED taxonomy in ``agent/tool_diagnostics``
rather than defining a parallel one (#745): ``classify_browser_error`` returns
one of the global ``tool_diagnostics`` categories (``timeout``, ``permission``,
``missing_command``, ``limit``, ``provider_dead``, ``not_found``,
``runtime_error``) so browser failures carry the same ``failure_class`` the
native tools and ``loop_guard`` already use. Browser-specific error strings the
generic classifier does not recognise (CDP/Chrome backend down, an absent tool
set, box-model/DOM read failures) are routed to the closest shared category
first, then anything else defers to ``tool_diagnostics.classify()``.

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

from agent.tool_diagnostics import classify as _diagnostics_classify

logger = logging.getLogger(__name__)

# Per-URL consecutive navigation-failure counts. After ``MAX_NAV_FAILURES`` the
# fallback layer stops re-attempting the browser for that URL and tells the agent
# to use the extracted text / web_search / report instead of looping. Reset on a
# successful navigation. Process-local; bounded by URL cardinality in a session.
_nav_failures: dict[str, int] = {}
MAX_NAV_FAILURES = 3

# Browser-specific error signals the generic ``tool_diagnostics`` regexes do not
# recognise (or would misclassify), mapped onto the SHARED taxonomy categories.
# Ordered most-specific first; first match wins. Each RHS is a real
# ``agent/tool_diagnostics`` category name — NOT a browser-only class (#745).
_BROWSER_ERROR_RULES: tuple[tuple[re.Pattern, str], ...] = (
    # A browser backend that exposes a different / absent tool set is a missing
    # command: the requested browser command is not available here. ``classify``
    # would call this ``not_found`` (change-and-retry); ``missing_command`` is
    # the deterministic non-retryable class, which is the correct signal.
    (re.compile(r"tool does not exist|available tools:|unknown ref|no such tool", re.I),
     "missing_command"),
    # CDP / Chrome backend down or unreachable is a dead provider: retrying the
    # same navigation keeps failing — route to the web_extract / web_search
    # fallback instead of re-driving a backend that is not there.
    (re.compile(
        r"\bCDP\b|chrome devtools|websocketdebugger|could not connect to (chrome|browser)"
        r"|browser (backend|session) (unavailable|not)",
        re.I,
    ),
     "provider_dead"),
    # Navigation / page timeouts.
    (re.compile(r"timed out|timeout|deadline exceeded|navigation timeout|Page\.navigate", re.I),
     "timeout"),
    # DOM / selector read failures are runtime errors against the live page.
    (re.compile(r"could not compute box model|detached|stale element|DOM|selector|element not found", re.I),
     "runtime_error"),
)

# Browser-context recovery hints keyed by the SHARED ``tool_diagnostics``
# category. The category is shared; the hint text is browser-flavoured (it
# points at the web_extract / web_search fallback instead of the generic
# advice). Categories without a browser-specific hint fall back to
# ``_DEFAULT_HINT``.
_TAXONOMY_HINT = {
    "missing_command": "The browser backend exposed a different tool set. Do NOT repeat the same "
                       "call — use the extracted text below, web_search, or report the gap.",
    "provider_dead": "The browser backend (CDP/Chrome) is unavailable. Retrying navigation will "
                     "keep failing — use the extracted text below or web_search for this URL.",
    "timeout": "Navigation timed out. This is deterministic for a slow/blocked page — "
               "use the extracted text below or web_search instead of re-navigating.",
    "runtime_error": "The page could not be read/navigated. Use the extracted text below, or fetch "
                     "via web_extract/web_search rather than re-driving the browser.",
}
_DEFAULT_HINT = _TAXONOMY_HINT["runtime_error"]


def classify_browser_error(error: Optional[str]) -> str:
    """Map a browser tool error string to a shared ``agent/tool_diagnostics``
    failure category.

    Reuses the global taxonomy rather than a parallel one (#745). Browser-only
    signals the generic classifier does not recognise are routed to the closest
    shared category first; otherwise defers to ``tool_diagnostics.classify()``;
    an unrecognised or empty failure defaults to ``runtime_error`` (the shared
    catch-all).
    """
    if not isinstance(error, str) or not error.strip():
        return "runtime_error"
    for pattern, category in _BROWSER_ERROR_RULES:
        if pattern.search(error):
            return category
    hit = _diagnostics_classify(error)
    if hit:
        return hit[0]
    return "runtime_error"


# Back-compat alias: navigation-specific callers keep the old name, but it now
# returns a shared ``tool_diagnostics`` category rather than a browser-only class.
classify_navigation_error = classify_browser_error


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
    """Build the structured browser_navigate failure result (#234/#213/#745):
    classify with the shared taxonomy, count toward the per-URL cap, attempt the
    web_extract fallback, and attach an actionable recovery directive."""
    klass = classify_browser_error(error)
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
        hint = _TAXONOMY_HINT.get(klass, _DEFAULT_HINT)
        if capped:
            hint = (
                f"Reached the {MAX_NAV_FAILURES}-attempt cap for this URL. STOP "
                f"navigating here. " + hint
            )
        response["recovery"] = hint
    return response
