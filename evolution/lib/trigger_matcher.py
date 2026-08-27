"""Deterministic trigger matching for the skill library (#3229)."""

from __future__ import annotations
import re
from typing import Any, List, Sequence, Tuple

TYPES = ("goal_contains", "tool_used", "error_class", "task_kind", "intent")
DEFAULT_WEIGHT = 1.0
_STOP = frozenset({
    "the",
    "a",
    "an",
    "and",
    "or",
    "of",
    "to",
    "for",
    "in",
    "on",
    "with",
    "this",
    "that",
    "it",
    "is",
    "are",
    "be",
    "do",
    "does",
    "please",
    "can",
    "i",
    "you",
    "we",
    "my",
    "your",
    "me",
    "help",
    "need",
    "want",
    "using",
    "use",
    "from",
    "by",
    "at",
    "as",
    "into",
    "about",
    "how",
    "what",
    "when",
})


def _tokens(text: str) -> List[str]:
    return [w for w in re.split(r"[^a-zA-Z0-9]+", text.lower()) if w and w not in _STOP]


def extract_triggers(trace: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(trace, dict):
        return []
    out: list[dict[str, Any]] = []
    if (
        isinstance(trace.get("goal"), str)
        and (g := trace["goal"]).strip()
        and (kw := _tokens(g))
    ):
        out.append({"type": "goal_contains", "values": kw, "weight": DEFAULT_WEIGHT})
    calls = trace.get("tool_calls")
    if isinstance(calls, list):
        tools: list[str] = []
        for c in calls:
            n = c.get("name") if isinstance(c, dict) else None
            if isinstance(n, str) and n and n not in tools:
                tools.append(n)
        if tools:
            out.append({"type": "tool_used", "values": tools, "weight": 0.8})
    return out


def parse_triggers(skill_markdown: str) -> list[dict[str, Any]]:
    if not isinstance(skill_markdown, str):
        return []
    lines = skill_markdown.splitlines()
    fm = ""
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                fm = "\n".join(lines[1:i])
                break
    if not fm:
        fm = skill_markdown
    triggers: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    inside = False
    for line in fm.splitlines():
        s = line.strip()
        if not inside:
            if s.startswith("triggers:"):
                inside = True
            continue
        if s.startswith("- "):
            if cur is not None:
                triggers.append(cur)
            cur, s = (
                {"type": None, "values": [], "weight": DEFAULT_WEIGHT},
                s[2:].strip(),
            )
        if cur is None:
            break
        if ":" not in s:
            continue
        k, _, v = s.partition(":")
        k, v = k.strip(), v.strip()
        if k == "type":
            cur["type"] = v.strip("'\"")
        elif k == "weight":
            try:
                cur["weight"] = float(v)
            except ValueError:
                pass
        elif k == "values":
            cur["values"] = [
                x.strip().strip("'\"") for x in v.strip("[]").split(",") if x.strip()
            ]
    if cur is not None:
        triggers.append(cur)
    return [t for t in triggers if t.get("type") in TYPES and t.get("values")]


def render_triggers(triggers: Sequence[dict[str, Any]]) -> str:
    if not triggers:
        return ""
    lines = ["triggers:"]
    for t in triggers:
        if (
            not isinstance(t, dict)
            or (tt := t.get("type")) not in TYPES
            or not isinstance((vals := t.get("values")), list)
            or not vals
        ):
            continue
        lines.append(f"  - type: {tt}")
        lines.append(f"    values: [{', '.join(str(v) for v in vals)}]")
        lines.append(f"    weight: {float(t.get('weight', DEFAULT_WEIGHT)):g}")
    return "\n".join(lines) if len(lines) > 1 else ""


def score_trigger(state: dict[str, Any], trigger: dict[str, Any]) -> float:
    """Score a single trigger condition against current execution state."""
    if not isinstance(state, dict) or not isinstance(trigger, dict):
        return 0.0
    tt, vals = trigger.get("type"), trigger.get("values")
    if tt not in TYPES or not isinstance(vals, list) or not vals:
        return 0.0
    try:
        w = float(trigger.get("weight", DEFAULT_WEIGHT))
    except (TypeError, ValueError):
        w = DEFAULT_WEIGHT

    if tt in ("goal_contains", "intent"):
        g = str(state.get("goal") or state.get("intent") or "").lower()
        if not g:
            return 0.0
        return w if any(isinstance(v, str) and v.lower() in g for v in vals) else 0.0

    if tt == "tool_used":
        tools = state.get("tools_used") or state.get("tool_constellation")
        if not isinstance(tools, (list, tuple, set)):
            return 0.0
        tset = {str(t).lower() for t in tools}
        return w if any(isinstance(v, str) and v.lower() in tset for v in vals) else 0.0

    if tt == "error_class":
        err = str(state.get("error_class") or state.get("last_error") or "").lower()
        if not err:
            return 0.0
        return w if any(isinstance(v, str) and v.lower() in err for v in vals) else 0.0

    if tt == "task_kind":
        kind = str(state.get("task_kind") or state.get("kind") or "").lower()
        if not kind:
            return 0.0
        return w if any(isinstance(v, str) and v.lower() in kind for v in vals) else 0.0

    return 0.0


def score_state_against_skill(state: dict[str, Any], skill: dict[str, Any]) -> float:
    """Score execution state against a skill's triggers, returning relevance 0.0..1.0."""
    if not isinstance(state, dict) or not isinstance(skill, dict):
        return 0.0
    triggers = skill.get("triggers")
    if not isinstance(triggers, list):
        md = skill.get("skill_markdown")
        triggers = parse_triggers(md) if isinstance(md, str) else []
    if not triggers:
        return 0.0
    scores = [score_trigger(state, t) for t in triggers]
    return max(scores, default=0.0)


def get_matching_skills(
    state: dict[str, Any],
    skills: Sequence[dict[str, Any]],
    threshold: float = 0.7,
    top_k: int = 3,
) -> list[tuple[dict[str, Any], float]]:
    """Retrieve top matching skills whose trigger scores meet or exceed threshold."""
    scored: list[tuple[dict[str, Any], float]] = []
    for skill in skills:
        if not isinstance(skill, dict):
            continue
        score = score_state_against_skill(state, skill)
        if score >= threshold:
            scored.append((skill, score))
    scored.sort(key=lambda item: (-item[1], str(item[0].get("name", ""))))
    return scored[:top_k]


def best_matches(
    state: dict[str, Any], skills: Sequence[dict[str, Any]], threshold: float = 0.7
) -> list[tuple[str, float]]:
    """Legacy helper returning name, score tuples."""
    ranked: list[tuple[str, float]] = []
    for skill, score in get_matching_skills(state, skills, threshold=threshold, top_k=100):
        name = str(skill.get("name", ""))
        if name:
            ranked.append((name, score))
    return ranked
