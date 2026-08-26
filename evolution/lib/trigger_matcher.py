# -*- coding: utf-8 -*-
"""Skill trigger extraction, validation and matching (first slice of #3210).

Crystallized skills (``skill_crystallizer.py``) become genuinely useful only
when they carry explicit **trigger conditions**: the state under which the
skill should be retrieved instead of re-deriving the workflow. This module is
the deterministic core of that loop:

1. ``extract_trigger_metadata`` derives trigger conditions from a trace
   (task kind + tool constellation + intent signals).
2. ``validate_trigger_metadata`` gates what may be written into SKILL.md
   frontmatter (inspectable, no free-form blobs).
3. ``score_trigger`` scores a runtime state against stored triggers and
   ``best_matches`` retrieves skills whose score clears a threshold.

The runtime monitor wiring (agent turn loop) and the co-evolution demotion
loop are deferred to later increments of #3210.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "DEFAULT_MATCH_THRESHOLD",
    "best_matches",
    "extract_trigger_metadata",
    "parse_trigger_frontmatter",
    "render_trigger_frontmatter",
    "score_trigger",
    "validate_trigger_metadata",
]

DEFAULT_MATCH_THRESHOLD = 0.5

# Score weights (sum to 1.0): task kind dominates, tool constellation second,
# intent signals are the weakest signal.
W_TASK_KIND = 0.4
W_TOOLS = 0.35
W_INTENT = 0.25

_MAX_TOOLS = 8
_MAX_INTENT_SIGNALS = 3


def _tools_of(trace_data: Dict[str, Any]) -> List[str]:
    """Ordered unique tool names from a trace's tool_calls/events."""
    tools: List[str] = []
    for tc in trace_data.get("tool_calls") or []:
        if not isinstance(tc, dict):
            continue
        name = tc.get("name") or tc.get("tool") or ""
        if isinstance(name, str) and name and name not in tools:
            tools.append(name)
    return tools


def extract_trigger_metadata(trace_data: Dict[str, Any]) -> Dict[str, Any]:
    """Derive deterministic trigger conditions from a successful trace.

    Pure: reads only whitelisted fields, never raw content beyond the goal
    string already used for the skill description."""
    if not isinstance(trace_data, dict):
        return {}
    goal = str(trace_data.get("goal") or trace_data.get("user_message") or "")
    tools = _tools_of(trace_data)[:_MAX_TOOLS]
    task_kind = str(trace_data.get("task_kind") or "").strip().lower()
    if not task_kind:
        # Deterministic fallback: derive from the goal's leading words.
        words = re.findall(r"[a-zA-Z]+", goal.lower())[:2]
        task_kind = "-".join(words) if words else "generic"
    signals_in = trace_data.get("intent_signals")
    signals = (
        [str(s).strip().lower() for s in signals_in if str(s).strip()]
        if isinstance(signals_in, list)
        else []
    )
    if not signals and goal:
        signals = [" ".join(re.findall(r"[a-zA-Z]+", goal.lower())[:4])]
    return {
        "task_kind": task_kind,
        "tools": tools,
        "intent_signals": signals[:_MAX_INTENT_SIGNALS],
    }


def validate_trigger_metadata(meta: Any) -> Tuple[bool, str]:
    """Gate what may be persisted as trigger frontmatter (#3210 step 2).

    A valid trigger block has a non-empty string ``task_kind``, a list of
    non-empty string ``tools`` and a list of non-empty string
    ``intent_signals``. Returns (ok, reason)."""
    if not isinstance(meta, dict):
        return False, "trigger metadata must be a mapping"
    tk = meta.get("task_kind")
    if not isinstance(tk, str) or not tk.strip():
        return False, "missing or empty task_kind"
    for key in ("tools", "intent_signals"):
        val = meta.get(key, [])
        if not isinstance(val, list):
            return False, f"{key} must be a list"
        if any(not isinstance(v, str) or not v.strip() for v in val):
            return False, f"{key} entries must be non-empty strings"
    return True, "valid"


def render_trigger_frontmatter(meta: Dict[str, Any]) -> str:
    """Render validated trigger metadata as an inspectable YAML block."""
    lines = ["triggers:", f"  task_kind: {meta['task_kind']}"]
    lines.append("  tools:")
    for t in meta.get("tools") or []:
        lines.append(f"    - {t}")
    lines.append("  intent_signals:")
    for s in meta.get("intent_signals") or []:
        lines.append(f"    - {s}")
    return "\n".join(lines)


def parse_trigger_frontmatter(markdown: str) -> Optional[Dict[str, Any]]:
    """Parse the ``triggers:`` block back out of a SKILL.md frontmatter.

    Returns ``None`` when no well-formed block exists (so callers can tell
    'no triggers' apart from 'malformed triggers')."""
    match = re.search(r"^triggers:\n((?:[ \t]+.*\n?)+)", markdown, flags=re.MULTILINE)
    if not match:
        return None
    body = match.group(1)
    tk = re.search(r"task_kind:\s*(\S+)", body)
    if not tk:
        return None

    def _list_of(key: str) -> List[str]:
        sec = re.search(rf"{key}:\n((?:[ \t]+-[^\n]*\n?)+)", body)
        if not sec:
            return []
        return [
            ln.split("-", 1)[1].strip() for ln in sec.group(1).splitlines() if "-" in ln
        ]

    meta = {
        "task_kind": tk.group(1),
        "tools": _list_of("tools"),
        "intent_signals": _list_of("intent_signals"),
    }
    ok, _ = validate_trigger_metadata(meta)
    return meta if ok else None


def score_trigger(state: Dict[str, Any], meta: Dict[str, Any]) -> float:
    """Score a runtime state against stored trigger conditions -> [0.0, 1.0].

    Components: exact ``task_kind`` match (0.4), Jaccard-ish overlap of the
    state's tools with the trigger's constellation (0.35), and any recorded
    intent signal appearing in the state's goal text (0.25). Pure."""
    if not isinstance(state, dict) or not isinstance(meta, dict):
        return 0.0
    score = 0.0
    if str(state.get("task_kind", "")).strip().lower() == str(
        meta.get("task_kind", "")
    ).strip().lower() and meta.get("task_kind"):
        score += W_TASK_KIND
    state_tools = {str(t).strip() for t in state.get("tools") or []}
    trig_tools = {str(t).strip() for t in meta.get("tools") or []}
    if state_tools and trig_tools:
        score += W_TOOLS * (len(state_tools & trig_tools) / len(trig_tools))
    goal = str(state.get("goal") or "").lower()
    if goal and any(str(s).lower() in goal for s in meta.get("intent_signals") or []):
        score += W_INTENT
    return round(min(1.0, score), 4)


def best_matches(
    state: Dict[str, Any],
    skill_triggers: List[Tuple[str, Dict[str, Any]]],
    threshold: float = DEFAULT_MATCH_THRESHOLD,
) -> List[Tuple[str, float]]:
    """Return ``(name, score)`` pairs clearing ``threshold``, best first."""
    scored = [(name, score_trigger(state, meta)) for name, meta in skill_triggers]
    hits = [(n, s) for n, s in scored if s >= threshold]
    hits.sort(key=lambda p: -p[1])
    return hits
