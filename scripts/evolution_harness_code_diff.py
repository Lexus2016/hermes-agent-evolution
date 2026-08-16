#!/usr/bin/env python3
"""Self-Harness code-diff proposal generator for the retry-policy surface (#2613, parent #2525).

Companion to ``evolution_harness_proposer.py``: turns a weakness record into a
structured CODE DIFF against the retry-policy surface (``retry_count`` /
``backoff`` / ``guard_conditions``), where the proposer only emits prose.

Emitted diff shape (the proposal schema): ``surface``, ``changes`` (list of
``{field, before, after, reason}`` knob edits), ``unified_diff``, ``evidence``,
plus ``source`` / ``status`` / ``requires_human_review`` / ``auto_apply``.
Human-gated, never auto-applied; nothing here writes a file or config.
"""
from __future__ import annotations

import difflib
from typing import Any, Dict, List, Optional

DIFFABLE_KINDS = ("retry_spiral", "provider_error")
RETRY_SPIRAL_CAP = 3


def _normalize_surface(surface: Any) -> Dict[str, Any]:
    s = surface if isinstance(surface, dict) else {}
    b = s.get("backoff") if isinstance(s.get("backoff"), dict) else {}
    guards = s.get("guard_conditions") if isinstance(s.get("guard_conditions"), list) else []
    return {
        "retry_count": int(s.get("retry_count", 3)),
        "backoff": {"base_delay_sec": float(b.get("base_delay_sec", 1.0)),
                    "multiplier": float(b.get("multiplier", 2.0)),
                    "max_delay_sec": float(b.get("max_delay_sec", 60.0))},
        "guard_conditions": [str(g) for g in guards],
    }


def _evidence_of(weakness: Dict[str, Any]) -> Dict[str, Any]:
    allowed = ("kind", "tool", "signature", "occurrences", "severity",
               "label", "max_consecutive", "sessions")
    return {k: weakness[k] for k in allowed if k in weakness}


def _render_diff(surface: Dict[str, Any], changes: List[Dict[str, Any]]) -> str:
    after = {"retry_count": surface["retry_count"], "backoff": dict(surface["backoff"]),
             "guard_conditions": list(surface["guard_conditions"])}
    for c in changes:
        after[c["field"]] = c["after"]
    return "".join(difflib.unified_diff(
        [f"{k}: {v}" for k, v in surface.items()], [f"{k}: {v}" for k, v in after.items()],
        fromfile="retry_policy (current)", tofile="retry_policy (proposed)", lineterm="\n"))


def build_retry_policy_diff(
    weakness: Dict[str, Any], surface: Optional[Dict[str, Any]] = None
) -> Optional[Dict[str, Any]]:
    """Return a structured retry-policy code diff for one weakness, or ``None``.

    Deterministic and inert: the returned dict follows the schema documented in
    the module docstring and carries the human-gating fields.
    """
    if not isinstance(weakness, dict) or weakness.get("kind") not in DIFFABLE_KINDS:
        return None
    s = _normalize_surface(surface)
    kind = weakness["kind"]
    changes: List[Dict[str, Any]] = []
    guards = list(s["guard_conditions"])

    if kind == "retry_spiral":
        if s["retry_count"] > RETRY_SPIRAL_CAP:
            changes.append({
                "field": "retry_count", "before": s["retry_count"],
                "after": RETRY_SPIRAL_CAP,
                "reason": "cap consecutive retries after a spiral "
                          "(observed %s consecutive)" % weakness.get("max_consecutive"),
            })
        guard = "non-retryable after %d consecutive attempts" % RETRY_SPIRAL_CAP
        if weakness.get("tool"):
            guard += f" for `{weakness['tool']}`"
        if guard not in guards:
            changes.append({"field": "guard_conditions", "before": list(guards),
                            "after": guards + [guard], "reason": "stop retrying past the cap"})
    else:  # provider_error
        b = s["backoff"]
        sig = weakness.get("signature")
        changes.append({
            "field": "backoff", "before": b,
            "after": {"base_delay_sec": b["base_delay_sec"] * 2.0,
                      "multiplier": b["multiplier"], "max_delay_sec": b["max_delay_sec"] * 2.0},
            "reason": f"widen backoff for recurring provider error `{sig}`",
        })
        guard = f"non-retryable error class: {sig}"
        if sig and guard not in guards:
            changes.append({"field": "guard_conditions", "before": list(guards),
                            "after": guards + [guard],
                            "reason": "mark recurring provider error class non-retryable"})

    if not changes:
        return None
    return {
        "surface": s,
        "changes": changes,
        "unified_diff": _render_diff(s, changes),
        "evidence": _evidence_of(weakness),
        "source": "self-harness",
        "status": "proposed",
        "requires_human_review": True,
        "auto_apply": False,
    }
