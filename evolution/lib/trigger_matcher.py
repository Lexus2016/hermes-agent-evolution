"""Deterministic trigger matching for the skill library (#3229)."""

from __future__ import annotations
import re
from typing import Any, List, Sequence, Tuple

TYPES = ("goal_contains", "tool_used")
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
    if not isinstance(state, dict) or not isinstance(trigger, dict):
        return 0.0
    tt, vals = trigger.get("type"), trigger.get("values")
    if tt not in TYPES or not isinstance(vals, list) or not vals:
        return 0.0
    try:
        w = float(trigger.get("weight", DEFAULT_WEIGHT))
    except (TypeError, ValueError):
        w = DEFAULT_WEIGHT
    if tt == "goal_contains":
        g = state.get("goal")
        if not isinstance(g, str):
            return 0.0
        g = g.lower()
        return w if any(isinstance(v, str) and v.lower() in g for v in vals) else 0.0
    tools = state.get("tools_used")
    if not isinstance(tools, list):
        return 0.0
    tset = {str(t).lower() for t in tools}
    return w if any(isinstance(v, str) and v.lower() in tset for v in vals) else 0.0


def best_matches(
    state: dict[str, Any], skills: Sequence[dict[str, Any]], threshold: float = 0.7
) -> list[tuple[str, float]]:
    ranked: list[tuple[str, float]] = []
    for skill in skills:
        if not isinstance(skill, dict):
            continue
        name = skill.get("name")
        if not isinstance(name, str) or not name:
            continue
        triggers = skill.get("triggers")
        if not isinstance(triggers, list):
            md = skill.get("skill_markdown")
            triggers = parse_triggers(md) if isinstance(md, str) else []
        best = max((score_trigger(state, t) for t in triggers), default=0.0)
        if best >= threshold:
            ranked.append((name, best))
    ranked.sort(key=lambda p: (-p[1], p[0]))
    return ranked
