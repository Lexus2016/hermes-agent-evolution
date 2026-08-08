"""task-shield plugin — heuristic pre-execution tool-call validator (#1798).

Hooks: pre_api_request (goal extraction), pre_tool_call (pre-dispatch check).
Blocks calls that look like injected instructions. Heuristic-only.
Env: TASK_SHIELD_DISABLE=1 off; TASK_SHIELD_WARN=1 warn-only.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)
_local = threading.local()
_TRUTHY = {"1", "true", "yes", "on"}

_ACTION_KEYWORDS: Dict[str, str] = {
    "write": "write create save update edit modify patch change set insert add replace",
    "send": "send email message post publish share forward reply notify tweet",
    "search": "search find look query locate google",
    "read": "read show display list view cat open get fetch check inspect",
    "execute": "run execute start launch deploy install build make do test",
}

# Communication tools — primary injection targets. Match by suffix patterns.
_HIGH_RISK_SUFFIXES = (
    "send_message",
    "create_draft",
    "reply_to_message",
    "forward_message",
    "create_tweet",
    "upload_media",
    "create_post",
    "upload_image",
)


def _is_high_risk(tool_name: str) -> bool:
    return any(tool_name.endswith(s) for s in _HIGH_RISK_SUFFIXES)


_SUSPICIOUS_PATTERNS: List[re.Pattern] = [
    re.compile(r"<\s*system\s*>", re.I),
    re.compile(r"ignore.*(?:previous|above).*instructions", re.I),
    re.compile(r"you\s+must\s+(?:now\s+)?(?:send|post|publish|forward|create)", re.I),
    re.compile(r"system\s+prompt.*(?:says|instructs)", re.I),
]


def _extract_goal_keywords(text: str) -> Set[str]:
    tl = text.lower()
    return {
        a for a, kws in _ACTION_KEYWORDS.items() if any(k in tl for k in kws.split())
    }


def _extract_goal_text(msg: Any) -> str:
    if isinstance(msg, str):
        return msg
    if isinstance(msg, dict):
        c = msg.get("content", "")
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            return " ".join(
                p.get("text", "") if isinstance(p, dict) else str(p) for p in c
            )
    return ""


def _detect_injection(args_text: str) -> List[str]:
    return [
        f"pattern: {p.pattern}" for p in _SUSPICIOUS_PATTERNS if p.search(args_text)
    ]


def _check_goal_consistency(
    tool_name: str, args: Any, goal_kws: Set[str]
) -> Optional[str]:
    """Return reason string if suspicious, None if fine."""
    if not goal_kws:
        return None  # no goal stored (system-initiated)
    args_text = " ".join(
        v if isinstance(v, str) else str(v)
        for v in (args.values() if isinstance(args, dict) else [args])
    )
    hits = _detect_injection(args_text)
    if hits:
        return f"Arguments contain prompt-injection patterns ({'; '.join(hits[:2])})."
    if _is_high_risk(tool_name) and "send" not in goal_kws:
        return (
            f"Tool '{tool_name}' sends external communication, but the user's "
            f"request does not mention sending/posting."
        )
    return None


# ── Hooks ───────────────────────────────────────────────────────────────────


def _on_pre_api_request(
    user_message: Any = None, session_id: str = "", **_: Any
) -> None:
    if os.getenv("TASK_SHIELD_DISABLE", "").strip().lower() not in _TRUTHY:
        goal_text = _extract_goal_text(user_message)
        if goal_text:
            setattr(_local, f"goal_{session_id}", goal_text[:500])


def _on_pre_tool_call(
    tool_name: str = "", args: Any = None, session_id: str = "", **_: Any
) -> Optional[Dict[str, str]]:
    if os.getenv("TASK_SHIELD_DISABLE", "").strip().lower() in _TRUTHY:
        return None
    goal_kws = _extract_goal_keywords(
        getattr(_local, f"goal_{session_id or 'default'}", "")
    )
    reason = _check_goal_consistency(tool_name, args, goal_kws)
    if reason is None:
        return None
    logger.warning("Task Shield flagged '%s': %s", tool_name, reason)
    if os.getenv("TASK_SHIELD_WARN", "").strip().lower() in _TRUTHY:
        return None  # warn-only
    return {
        "action": "block",
        "message": (
            "⛔ Task Shield: blocked — this call may serve an injected instruction, "
            f"not the user's request.\n\nReason: {reason}\n\n"
            "If legitimate, rephrase your request or set TASK_SHIELD_WARN=1."
        ),
    }


def register(ctx) -> None:
    ctx.register_hook("pre_api_request", _on_pre_api_request)
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
