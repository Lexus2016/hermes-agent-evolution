#!/usr/bin/env python3
"""Verify-before-retry wrapper for non-atomic tool failures (issue #1924).

When a side-effecting MCP tool call fails after the effect was dispatched
(timeout-after-dispatch, eventual-consistency lag), the agent's naive retry
produces duplicate real-world side effects: duplicate emails, duplicate
GitHub issues, duplicate tweets.

This module provides the verify-before-retry pattern: before retrying a
failed side-effecting call, verify whether the intended effect already
occurred. Only retry if verification confirms the effect did NOT happen.

The four-mode non-atomic failure taxonomy:
1. Timeout-after-dispatch — effect likely occurred; retry would duplicate.
2. Eventual consistency — write succeeded, read-back hasn't propagated yet.
3. Stale-version conflict — effect did NOT occur; retry is correct.
4. Partial state update — some fields applied, others didn't.

Usage:
    from scripts.evolution_verify_before_retry import should_retry, classify_failure

    # Before retrying a failed tool call:
    if should_retry(tool_name, args, error, verify_fn=check_effect):
        retry(tool_name, args)
    else:
        return cached_result  # effect already occurred
"""

from __future__ import annotations
import hashlib
import json
from typing import Any, Callable, Dict, Optional

# Tools that have real-world side effects and are non-atomic.
SIDE_EFFECTING_TOOLS = {
    "agentmail__send_message",
    "github__create_issue",
    "github__create_repo",
    "x_twitter__create_tweet",
    "x_twitter__upload_media",
    "gmail__send_message",
    "linkedin__create_post",
}

# Error signatures that indicate timeout-after-dispatch (effect may have occurred).
_TIMEOUT_INDICATORS = {"timeout", "timed out", "connection reset", "read timeout"}

# Error signatures that indicate the effect did NOT occur (safe to retry).
_SAFE_RETRY_INDICATORS = {
    "404",
    "not found",
    "authentication",
    "unauthorized",
    "forbidden",
}


def classify_failure(error: str) -> str:
    """Classify a tool failure into one of the four non-atomic failure modes.

    Returns one of: 'timeout-after-dispatch', 'eventual-consistency',
    'stale-version-conflict', 'partial-state-update', 'safe-retry', 'unknown'.
    """
    err_lower = error.lower() if error else ""
    if any(sig in err_lower for sig in _TIMEOUT_INDICATORS):
        return "timeout-after-dispatch"
    if "conflict" in err_lower or "etag" in err_lower or "412" in err_lower:
        return "stale-version-conflict"
    if "partial" in err_lower or "incomplete" in err_lower:
        return "partial-state-update"
    if (
        "consistency" in err_lower
        or "propagated" in err_lower
        or "eventually" in err_lower
    ):
        return "eventual-consistency"
    if any(sig in err_lower for sig in _SAFE_RETRY_INDICATORS):
        return "safe-retry"
    return "unknown"


def is_side_effecting(tool_name: str) -> bool:
    """Check if a tool has real-world side effects (non-atomic)."""
    return tool_name in SIDE_EFFECTING_TOOLS


def generate_idempotency_key(
    tool_name: str, args: Dict[str, Any], nonce: str = ""
) -> str:
    """Generate a deterministic idempotency key for a tool call.

    APIs that support idempotency (AgentMail, Stripe, many others) will
    deduplicate automatically when the same key is replayed.
    """
    canonical = json.dumps({"tool": tool_name, "args": args}, sort_keys=True)
    return hashlib.sha256(f"{canonical}:{nonce}".encode()).hexdigest()[:32]


def should_retry(
    tool_name: str,
    args: Dict[str, Any],
    error: str,
    verify_fn: Optional[Callable[[Dict[str, Any]], bool]] = None,
) -> bool:
    """Determine whether a failed tool call should be retried.

    For side-effecting tools, if verify_fn is provided, it is called to check
    whether the effect already occurred. If the effect occurred (verify_fn
    returns True), do NOT retry — return the cached result instead.

    For non-side-effecting tools, always retry (the failure is transient).

    Args:
        tool_name: The tool that failed.
        args: The arguments passed to the tool.
        error: The error message from the failed call.
        verify_fn: Optional callback that checks if the effect occurred.
            Returns True if effect was applied, False if not.

    Returns:
        True if the call should be retried, False if the effect already occurred.
    """
    if not is_side_effecting(tool_name):
        return True  # non-side-effecting tools are always safe to retry

    failure_mode = classify_failure(error)

    # Stale-version conflicts and auth errors: effect did NOT occur, safe to retry.
    if failure_mode in ("safe-retry", "stale-version-conflict"):
        return True

    # Timeout-after-dispatch and eventual-consistency: effect MAY have occurred.
    # Only retry if verification confirms the effect did NOT happen.
    if failure_mode in (
        "timeout-after-dispatch",
        "eventual-consistency",
        "partial-state-update",
    ):
        if verify_fn is not None:
            try:
                effect_occurred = verify_fn(args)
                return not effect_occurred  # retry only if effect did NOT occur
            except Exception:
                # If verification fails, err on the side of caution: don't retry
                # (avoids duplicate side effects). The caller can retry manually
                # after checking.
                return False
        # No verification function: be cautious, don't retry blindly
        return False

    # Unknown failure: default to retry (transient errors are most common)
    return True


# Verification function templates for the three highest-risk tools.


def verify_agentmail_sent(args: Dict[str, Any]) -> bool:
    """Template: verify an email was sent by checking if a matching message exists.
    In production, this would call agentmail__list_messages and search for
    a message with matching subject + recipient + timestamp window.
    """
    # This is a template — the actual implementation would use the MCP tool:
    #   messages = agentmail__list_messages(inbox_id=args.get("inbox_id"))
    #   return any(m["subject"] == args.get("subject") and
    #              args.get("to") in m.get("recipients", [])
    #              for m in messages)
    return False  # placeholder — override in production


def verify_github_issue_created(args: Dict[str, Any]) -> bool:
    """Template: verify a GitHub issue was created by searching for it.
    In production, this would call github__list_issues and search for
    a matching title + recent creation time.
    """
    #   issues = github__list_issues(repo=args.get("repo"))
    #   return any(i["title"] == args.get("title") for i in issues)
    return False  # placeholder — override in production


def verify_tweet_posted(args: Dict[str, Any]) -> bool:
    """Template: verify a tweet was posted by checking user tweets.
    In production, this would call x_twitter__get_user_tweets and search
    for a tweet with matching text + recent timestamp.
    """
    #   tweets = x_twitter__get_user_tweets()
    #   return any(t["text"] == args.get("text") for t in tweets)
    return False  # placeholder — override in production


# Registry mapping tool names to their verification functions.
VERIFY_FUNCTIONS: Dict[str, Callable[[Dict[str, Any]], bool]] = {
    "agentmail__send_message": verify_agentmail_sent,
    "github__create_issue": verify_github_issue_created,
    "x_twitter__create_tweet": verify_tweet_posted,
}


def get_verify_fn(tool_name: str) -> Optional[Callable[[Dict[str, Any]], bool]]:
    """Get the verification function for a side-effecting tool, if one exists."""
    return VERIFY_FUNCTIONS.get(tool_name)
