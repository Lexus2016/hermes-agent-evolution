#!/usr/bin/env python3
"""Refusal taxonomy and recovery (#756): classify refusals, detect in session
text, suggest recovery. CLI call sites — no dead code."""

from __future__ import annotations

import json
import re
import sys
from typing import Any, Dict, List

TRUE_CAPABILITY_GAP = "true_capability_gap"
OVER_REFUSAL = "over_refusal"
MISSING_SKILL_TRIGGER = "missing_skill_trigger"
PERMISSION_BOUNDARY = "permission_boundary"
RECOVERABLE_ERROR = "recoverable_error"
SPURIOUS = "spurious"

_REFUSAL_PAT = re.compile(
    "|".join([  # fmt: skip
        r"i can'?t",
        r"i can not",
        r"i don'?t have (?:access|permission)",
        r"no access",
        r"i'?m unable to",
        r"i don'?t have (?:a |the )?(?:tool|skill|feature|plugin|ability|capability)",
        r"i cannot (?:help|assist|do|provide|access)",
        r"not (?:able|allowed) to",
    ]),
    re.IGNORECASE,
)
_FP_RE = re.compile(
    r"i can'?t (?:stress|emphasize|overstate|imagine|believe|say|thank|praise|wait)",
    re.IGNORECASE,
)
_RECOVERY: Dict[str, str] = {  # fmt: skip
    TRUE_CAPABILITY_GAP: "Install or configure the missing capability, or route to a tool that has it.",
    OVER_REFUSAL: "The capability exists locally — use the available tool directly.",
    MISSING_SKILL_TRIGGER: "Activate the relevant skill with /skills install or load it explicitly.",
    PERMISSION_BOUNDARY: "Legitimate security boundary — explain it and suggest an alternative.",
    RECOVERABLE_ERROR: "Retry with a different approach — the task is achievable via an alternative path.",
    SPURIOUS: "Not an actual refusal — no action needed.",
}


def detect_refusals(text: str) -> List[Dict[str, Any]]:
    """Detect refusal phrases in text, filtering rhetorical false positives."""
    results: List[Dict[str, Any]] = []
    for m in _REFUSAL_PAT.finditer(text):
        snippet = text[m.start() : m.start() + 40]
        results.append({"phrase": m.group(0), "start": m.start(), "end": m.end(), "is_refusal": not _FP_RE.match(snippet)})  # fmt: skip
    return results


def classify_refusal(
    text: str, *, has_tool_failure: bool = False, skill_available: bool = False
) -> str:
    """Classify a refusal into one of 6 categories."""
    if not any(r["is_refusal"] for r in detect_refusals(text)):
        return SPURIOUS
    if re.search(
        r"don'?t have (?:a |the )?(?:tool|skill|plugin|feature)", text, re.IGNORECASE
    ):
        return TRUE_CAPABILITY_GAP
    if re.search(r"permission|security|unauthorized|forbidden", text, re.IGNORECASE):
        return PERMISSION_BOUNDARY
    if has_tool_failure:
        return RECOVERABLE_ERROR
    if skill_available:
        return MISSING_SKILL_TRIGGER
    return OVER_REFUSAL


def recovery_suggestion(category: str) -> str:
    return _RECOVERY.get(category, _RECOVERY[SPURIOUS])


def analyze_session(
    text: str, *, has_tool_failure: bool = False, skill_available: bool = False
) -> Dict[str, Any]:
    """Analyze a session for refusals and return a report with recovery suggestions."""
    refusals = [r for r in detect_refusals(text) if r["is_refusal"]]
    if not refusals:
        return {"refusal_count": 0, "categories": [], "suggestions": [], "has_refusals": False}  # fmt: skip
    cats: List[str] = []
    for r in refusals:
        cat = classify_refusal(text[r["start"] : r["end"] + 80], has_tool_failure=has_tool_failure, skill_available=skill_available)  # fmt: skip
        if cat not in cats:
            cats.append(cat)
    return {"refusal_count": len(refusals), "categories": cats, "suggestions": [recovery_suggestion(c) for c in cats], "has_refusals": True}  # fmt: skip


def main(argv: List[str]) -> int:
    if len(argv) < 2 or argv[1] in ("-h", "--help"):
        print(
            "usage: evolution_refusal_taxonomy.py {detect,classify,analyze} ...",
            file=sys.stderr,
        )
        return 2
    cmd, args = argv[1], argv[2:]
    if cmd == "classify":
        print(
            json.dumps(
                {"category": classify_refusal(" ".join(args))}, ensure_ascii=False
            )
        )
        return 0
    if cmd == "detect":
        text = args[0] if args else sys.stdin.read()
        print(json.dumps(detect_refusals(text), ensure_ascii=False))
        return 0
    if cmd == "analyze":
        text = args[0] if args else sys.stdin.read()
        print(json.dumps(analyze_session(text), ensure_ascii=False))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
