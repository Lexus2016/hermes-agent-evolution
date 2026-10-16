"""Versioned routing contracts and protected instruction replay (#3462).

These deterministic checks validate applicability behavior and literal instruction
preservation. They do not claim to validate arbitrary natural-language outcomes.
"""
from __future__ import annotations

import re

MAX_CASES = 32
MAX_PHRASES = 16
MAX_TEXT = 512


def _phrases(value, field):
    if not isinstance(value, list) or len(value) > MAX_PHRASES:
        raise ValueError(f"contract.{field} must be a list of at most {MAX_PHRASES} phrases")
    if any(not isinstance(v, str) or not v.strip() or len(v) > MAX_TEXT for v in value):
        raise ValueError(f"contract.{field} contains an empty, oversized, or non-text phrase")
    return value


def validate_contract(contract):
    if not isinstance(contract, dict):
        raise ValueError("contract must be a mapping")
    if type(contract.get("version")) is not int or contract["version"] < 1:
        raise ValueError("contract.version must be a positive integer")
    intent = contract.get("intent")
    if not isinstance(intent, str) or not intent.strip() or len(intent) > MAX_TEXT:
        raise ValueError("contract.intent must be non-empty bounded text")
    if contract.get("behavioral_criteria") != ["routing-applicability"]:
        raise ValueError("supported contract behavioral_criteria: [routing-applicability]")
    for field in ("applies_to", "excludes", "protected_constraints"):
        _phrases(contract.get(field), field)
    cases = contract.get("replay_cases")
    if not isinstance(cases, list) or not 1 <= len(cases) <= MAX_CASES:
        raise ValueError(f"contract.replay_cases must contain 1..{MAX_CASES} cases")
    seen = set()
    for case in cases:
        if not isinstance(case, dict) or set(case) != {"id", "task", "applicable"}:
            raise ValueError("replay case requires id, task, applicable")
        if any(not isinstance(case[k], str) or not case[k].strip() or len(case[k]) > MAX_TEXT for k in ("id", "task")):
            raise ValueError("replay case id/task must be bounded non-empty text")
        if case["id"] in seen or case["applicable"] is not None and type(case["applicable"]) is not bool:
            raise ValueError("replay ids must be unique; applicable must be true, false, or null (unknown)")
        seen.add(case["id"])
    return contract


def applicability(contract, task):
    """False only for explicit exclusion with no positive scope; ambiguity stays usable."""
    if contract is None:
        return None
    try:
        validate_contract(contract)
    except ValueError:
        return None  # malformed/unknown metadata cannot hide a legacy skill
    def matches(phrase):
        return re.search(r"(?<!\w)" + re.escape(phrase.casefold()) + r"(?!\w)", task.casefold()) is not None
    positive = any(matches(p) for p in contract["applies_to"])
    negative = any(matches(p) for p in contract["excludes"])
    if positive and negative:
        return None
    if negative:
        return False
    return True if positive else None


def check_contract_edit(content, original=None):
    """Return a diagnostic before mutation, or None. Replay baseline cases, not self-grades."""
    from agent.skill_utils import parse_frontmatter
    try:
        fm, body = parse_frontmatter(content)
        old, _ = parse_frontmatter(original) if original is not None else ({}, "")
        prior = old.get("contract")
        current = fm.get("contract")
        if "contract" not in fm and "contract" not in old:
            return None
        validate_contract(current)
        if prior is not None:
            validate_contract(prior)
            if fm.get("name") != old.get("name"):
                raise ValueError("contracted skill identity cannot change during an edit")
            if content != original and current["version"] <= prior["version"]:
                raise ValueError("contract.version must increase on every skill edit")
            if not set(prior["protected_constraints"]) <= set(current["protected_constraints"]):
                raise ValueError("cannot remove protected constraints")
            current_cases = {c["id"]: c for c in current["replay_cases"]}
            if any(current_cases.get(c["id"]) != c for c in prior["replay_cases"]):
                raise ValueError("cannot remove or rewrite historical replay cases")
        for text in current["protected_constraints"]:
            if text not in body:
                raise ValueError(f"protected instruction missing: {text}")
        for case in current["replay_cases"]:
            if applicability(current, case["task"]) is not case["applicable"]:
                raise ValueError(f"routing replay failed: {case['id']}")
    except (ValueError, TypeError) as exc:
        return f"Skill contract rejected: {exc}"
    return None
