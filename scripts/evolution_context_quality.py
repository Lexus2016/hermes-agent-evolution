#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Preflight context-engineering quality check (issue #1163).

A non-circular, model-free scorer that grades a *context artifact* (a stage's
role text, guardrails, instructions, tool schemas, retrieved grounding,
untrusted-input handling, token budget) across the seven criteria validated by
"AI Agents Do Not Fail Alone: The Context Fails First" (arXiv:2607.14275) as a
leading indicator of agent reliability:

    role_clarity, guardrail_coverage, instruction_consistency, tool_schema_quality,
    grounding_sufficiency, injection_hardening, token_efficiency

The critical methodological property from the paper is preserved: the score is
**isolated from behavioral metrics and the release decision** (non-circular). The
gate FLAGS regressions; it never auto-approves. It is a cheap complement to the
existing on-task verification, catching changes that pass the targeted check but
weaken the context surface (guardrails / tool-schema / instruction consistency).

Design (matching the ``scripts/evolution_*.py`` corpus): pure functions +
dataclasses, standard library only, no side effects on import, JSON-serialisable
records, an ``evaluate()`` core and a ``main()`` CLI. Deliberately **no LLM** and
**no external harness dependency** — a deterministic heuristic scorer is sound to
run as a preflight gate; a model-based juror can be layered later behind the same
interface.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

__all__ = [
    "Criterion",
    "CriterionScore",
    "ContextQualityReport",
    "score_context",
    "compare_context",
    "evaluate",
    "main",
]


class Criterion(str, Enum):
    role_clarity = "role_clarity"
    guardrail_coverage = "guardrail_coverage"
    instruction_consistency = "instruction_consistency"
    tool_schema_quality = "tool_schema_quality"
    grounding_sufficiency = "grounding_sufficiency"
    injection_hardening = "injection_hardening"
    token_efficiency = "token_efficiency"


@dataclass(frozen=True)
class CriterionScore:
    criterion: Criterion
    score: float  # 0..1
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"criterion": self.criterion.value, "score": round(self.score, 3), "reason": self.reason}


@dataclass(frozen=True)
class ContextQualityReport:
    scores: tuple[CriterionScore, ...] = ()

    @property
    def combined(self) -> float:
        return round(sum(s.score for s in self.scores) / len(self.scores), 3) if self.scores else 0.0

    def by_criterion(self) -> dict[Criterion, float]:
        return {s.criterion: s.score for s in self.scores}

    def to_dict(self) -> dict[str, Any]:
        return {"combined": self.combined, "scores": [s.to_dict() for s in self.scores]}


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


def _text(v: Any) -> str:
    return v if isinstance(v, str) else ("" if v is None else str(v))


# Heuristic per-criterion scorers. Each maps a context dict to 0..1. They are
# intentionally simple, monotone, and explainable — the point is a cheap,
# non-circular *relative* signal (does a change regress a criterion?), not an
# absolute truth. Keys are optional; a missing signal scores low, not errors.

def _score_role_clarity(ctx: Mapping[str, Any]) -> CriterionScore:
    role = _text(ctx.get("role"))
    n = len(role.split())
    score = _clamp(n / 40.0) if n else 0.0
    return CriterionScore(Criterion.role_clarity, score, f"role has {n} words")


def _score_guardrail_coverage(ctx: Mapping[str, Any]) -> CriterionScore:
    guards = ctx.get("guardrails") or []
    if isinstance(guards, str):
        guards = [g for g in guards.splitlines() if g.strip()]
    n = len(guards) if hasattr(guards, "__len__") else 0
    score = _clamp(n / 5.0)
    return CriterionScore(Criterion.guardrail_coverage, score, f"{n} guardrail entries")


def _score_instruction_consistency(ctx: Mapping[str, Any]) -> CriterionScore:
    instrs = ctx.get("instructions") or []
    if isinstance(instrs, str):
        instrs = [i for i in instrs.splitlines() if i.strip()]
    conflicts = ctx.get("instruction_conflicts", 0)
    try:
        conflicts = int(conflicts)
    except (TypeError, ValueError):
        conflicts = 0
    base = 1.0 if instrs else 0.5
    score = _clamp(base - 0.34 * conflicts)
    return CriterionScore(Criterion.instruction_consistency, score, f"{conflicts} detected conflicts")


def _score_tool_schema_quality(ctx: Mapping[str, Any]) -> CriterionScore:
    tools = ctx.get("tool_schemas") or []
    if not hasattr(tools, "__iter__") or isinstance(tools, (str, bytes)):
        return CriterionScore(Criterion.tool_schema_quality, 0.0, "no tool schemas")
    tools = list(tools)
    if not tools:
        return CriterionScore(Criterion.tool_schema_quality, 1.0, "no tools to describe")
    described = 0
    for t in tools:
        fn = t.get("function", t) if isinstance(t, Mapping) else {}
        if isinstance(fn, Mapping) and _text(fn.get("description")).strip() and fn.get("parameters") is not None:
            described += 1
    score = _clamp(described / len(tools))
    return CriterionScore(Criterion.tool_schema_quality, score, f"{described}/{len(tools)} tools fully described")


def _score_grounding_sufficiency(ctx: Mapping[str, Any]) -> CriterionScore:
    grounding = ctx.get("grounding") or ctx.get("retrieved_memory") or []
    if isinstance(grounding, str):
        n = 1 if grounding.strip() else 0
    else:
        n = len(grounding) if hasattr(grounding, "__len__") else 0
    score = _clamp(n / 3.0)
    return CriterionScore(Criterion.grounding_sufficiency, score, f"{n} grounding sources")


def _score_injection_hardening(ctx: Mapping[str, Any]) -> CriterionScore:
    handles_untrusted = bool(ctx.get("untrusted_input_handling"))
    score = 1.0 if handles_untrusted else _clamp(0.3)
    return CriterionScore(
        Criterion.injection_hardening,
        score,
        "untrusted-input handling present" if handles_untrusted else "no untrusted-input handling declared",
    )


def _score_token_efficiency(ctx: Mapping[str, Any]) -> CriterionScore:
    tokens = ctx.get("token_count")
    budget = ctx.get("token_budget")
    try:
        tokens = float(tokens)
        budget = float(budget)
    except (TypeError, ValueError):
        return CriterionScore(Criterion.token_efficiency, 0.5, "token_count/budget not provided")
    if budget <= 0:
        return CriterionScore(Criterion.token_efficiency, 0.5, "invalid budget")
    ratio = tokens / budget
    score = _clamp(1.0 - max(0.0, ratio - 1.0)) if ratio > 1.0 else _clamp(1.0 - 0.2 * ratio)
    return CriterionScore(Criterion.token_efficiency, score, f"{tokens:g}/{budget:g} tokens (ratio {ratio:.2f})")


_SCORERS = (
    _score_role_clarity,
    _score_guardrail_coverage,
    _score_instruction_consistency,
    _score_tool_schema_quality,
    _score_grounding_sufficiency,
    _score_injection_hardening,
    _score_token_efficiency,
)


def score_context(context: Mapping[str, Any]) -> ContextQualityReport:
    """Score a context artifact across all seven criteria (non-circular, no LLM)."""
    ctx = context if isinstance(context, Mapping) else {}
    return ContextQualityReport(scores=tuple(s(ctx) for s in _SCORERS))


@dataclass(frozen=True)
class RegressionFinding:
    criterion: Criterion
    before: float
    after: float
    delta: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "criterion": self.criterion.value,
            "before": round(self.before, 3),
            "after": round(self.after, 3),
            "delta": round(self.delta, 3),
        }


def compare_context(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    *,
    threshold: float = 0.1,
) -> dict[str, Any]:
    """Flag criteria that regressed from ``before`` to ``after`` beyond ``threshold``.

    Returns a report with ``regressed`` (list of findings) and ``blocked`` (True
    if any criterion regressed). The gate FLAGS — the caller decides; the score is
    never coupled to the task-verification signal (non-circular).
    """
    b = score_context(before).by_criterion()
    a = score_context(after).by_criterion()
    regressed: list[RegressionFinding] = []
    for crit in Criterion:
        delta = a.get(crit, 0.0) - b.get(crit, 0.0)
        if delta < -abs(threshold):
            regressed.append(RegressionFinding(crit, b.get(crit, 0.0), a.get(crit, 0.0), delta))
    return {
        "blocked": bool(regressed),
        "threshold": threshold,
        "before_combined": score_context(before).combined,
        "after_combined": score_context(after).combined,
        "regressed": [f.to_dict() for f in regressed],
    }


def evaluate(before: Mapping[str, Any] | None, after: Mapping[str, Any], *, threshold: float = 0.1) -> dict[str, Any]:
    """Core entry: score ``after`` and, if ``before`` given, flag regressions."""
    report = score_context(after)
    result: dict[str, Any] = {"report": report.to_dict()}
    if before is not None:
        result["comparison"] = compare_context(before, after, threshold=threshold)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Preflight context-quality scorer (#1163)")
    parser.add_argument("--after", required=True, help="path to the after-context JSON")
    parser.add_argument("--before", help="path to the before-context JSON (enables regression check)")
    parser.add_argument("--threshold", type=float, default=0.1)
    args = parser.parse_args(argv)

    with open(args.after, encoding="utf-8") as fh:
        after = json.load(fh)
    before = None
    if args.before:
        with open(args.before, encoding="utf-8") as fh:
            before = json.load(fh)

    result = evaluate(before, after, threshold=args.threshold)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    # Exit non-zero only signals a flagged regression; it does not auto-approve.
    return 1 if result.get("comparison", {}).get("blocked") else 0


if __name__ == "__main__":
    sys.exit(main())
