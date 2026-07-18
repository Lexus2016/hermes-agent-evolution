# -*- coding: utf-8 -*-
"""Multi-hypothesis failure diagnosis for the tool-failure handler.

Second slice of SAGE-style Multi-Hypothesis Failure Attribution (issue #1029,
child of #994). Where the recovery dispatcher (#1027) commits to a single
category, this produces a *ranked list of hypotheses* about why a tool call
failed so the agent can weigh alternatives before retrying — instead of anchoring
on the first pattern that matched.

The hypotheses are derived from the **real** cross-tool classifier rule table
(``tools.tool_failure_classifier.matched_categories``): every category whose
pattern matches the error contributes a hypothesis, ranked by match strength
with the primary classification first. Confidence is rank-derived and
deterministic.

Design goals (matching ``agent/tool_guardrails.py`` and
``tools/recovery_strategy_dispatcher.py``):

* Pure functions + frozen dataclasses; **no side effects on import**.
* Standard library only; full type hints; ``from __future__ import annotations``.
* JSON serialisation on every dataclass.
* The runtime seam (:func:`maybe_append_diagnosis`) is config-gated and returns
  the result byte-for-byte unchanged unless the mode is active AND the call
  failed, so the default path introduces no behavior change.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from tools.tool_failure_classifier import (
    ToolFailureCategory,
    classify_tool_failure,
    matched_categories,
)

__all__ = [
    "DiagnosisMode",
    "Hypothesis",
    "Diagnosis",
    "HypothesisHistory",
    "diagnose_failure",
    "maybe_append_diagnosis",
    "DIAGNOSIS_GUIDANCE_PREFIX",
]

DIAGNOSIS_GUIDANCE_PREFIX = "Failure diagnosis"


class DiagnosisMode(str, Enum):
    """Config values for ``failure_diagnosis.mode``.

    ``off``              — no diagnosis (default).
    ``reflect``          — single best hypothesis only (a lightweight reflection).
    ``multi_hypothesis`` — full ranked list of hypotheses.
    """

    off = "off"
    reflect = "reflect"
    multi_hypothesis = "multi-hypothesis"

    @classmethod
    def coerce(cls, value: Any) -> "DiagnosisMode":
        if isinstance(value, cls):
            return value
        text = str(value or "").strip().lower().replace("_", "-")
        for member in cls:
            if member.value == text:
                return member
        return cls.off


# Rank-derived confidence: the primary hypothesis is the most confident, each
# subsequent one less so. Deterministic, no model call.
_RANK_CONFIDENCE = (0.7, 0.5, 0.35, 0.25, 0.15)


@dataclass(frozen=True)
class Hypothesis:
    """One ranked explanation of a tool failure."""

    category: ToolFailureCategory
    rank: int  # 1 = most likely
    confidence: float
    hint: str
    should_retry: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category.value,
            "rank": self.rank,
            "confidence": self.confidence,
            "hint": self.hint,
            "should_retry": self.should_retry,
        }


@dataclass(frozen=True)
class Diagnosis:
    """A ranked multi-hypothesis diagnosis for a failed tool call."""

    tool_name: str
    hypotheses: tuple[Hypothesis, ...] = ()

    @property
    def primary(self) -> Hypothesis | None:
        return self.hypotheses[0] if self.hypotheses else None

    def top(self, n: int) -> tuple[Hypothesis, ...]:
        return self.hypotheses[: max(0, n)]

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "hypotheses": [h.to_dict() for h in self.hypotheses],
        }


def _confidence_for_rank(rank: int) -> float:
    idx = rank - 1
    if 0 <= idx < len(_RANK_CONFIDENCE):
        return _RANK_CONFIDENCE[idx]
    return _RANK_CONFIDENCE[-1]


class HypothesisHistory:
    """Per-session record of diagnosis hypotheses already surfaced (issue #1030).

    Tracks, per failure key, which categories have already been proposed so a
    *repeated* failure demotes them and surfaces a NEW top hypothesis instead of
    re-proposing one the agent already acted on. Owned by a single agent session
    and reset per turn; not thread-safe by design.
    """

    def __init__(self) -> None:
        self._tried: dict[str, list[str]] = {}

    def tried(self, key: str) -> tuple[ToolFailureCategory, ...]:
        return tuple(ToolFailureCategory(v) for v in self._tried.get(key, ()))

    def record(self, key: str, category: ToolFailureCategory) -> None:
        bucket = self._tried.setdefault(key, [])
        if category.value not in bucket:
            bucket.append(category.value)

    def reset(self) -> None:
        self._tried.clear()

    def to_dict(self) -> dict[str, list[str]]:
        return {k: list(v) for k, v in self._tried.items()}


def diagnose_failure(
    tool_name: str,
    error: str = "",
    *,
    exit_code: int | None = None,
    consecutive_count: int = 0,
    stdout: str = "",
    stderr: str = "",
    max_hypotheses: int = 3,
    history: "HypothesisHistory | None" = None,
    history_key: str | None = None,
) -> Diagnosis:
    """Produce a ranked multi-hypothesis diagnosis for a failed tool call.

    The primary classification (from the same classifier the recovery dispatcher
    uses) is hypothesis #1. Additional categories whose patterns also matched the
    error text are ranked after it, deduplicated, best-first.

    When ``history`` and ``history_key`` are supplied (issue #1030), categories
    already surfaced for that key are **demoted** below fresh ones, so a repeated
    failure yields a new top hypothesis; the resulting primary is then recorded
    so the next repeat demotes it in turn.
    """
    primary = classify_tool_failure(
        tool_name,
        error,
        exit_code=exit_code,
        consecutive_count=consecutive_count,
        stdout=stdout,
        stderr=stderr,
    )

    ordered: list[ToolFailureCategory] = [primary.category]
    text = "\n".join(part for part in (error, stdout, stderr) if part)
    for category in matched_categories(text):
        if category not in ordered:
            ordered.append(category)

    # #1030 — demote already-tried categories so repeats surface fresh hypotheses.
    if history is not None and history_key is not None:
        tried = set(history.tried(history_key))
        if tried:
            fresh = [c for c in ordered if c not in tried]
            stale = [c for c in ordered if c in tried]
            ordered = fresh + stale

    from tools.tool_failure_classifier import (
        _CATEGORY_HINTS,
        _CATEGORY_RETRYABLE,
    )

    hypotheses: list[Hypothesis] = []
    for rank, category in enumerate(ordered[: max(1, max_hypotheses)], start=1):
        should_retry = (
            primary.should_retry
            if category is primary.category
            else _CATEGORY_RETRYABLE[category]
        )
        hypotheses.append(
            Hypothesis(
                category=category,
                rank=rank,
                confidence=_confidence_for_rank(rank),
                hint=_CATEGORY_HINTS[category],
                should_retry=should_retry,
            )
        )

    if history is not None and history_key is not None and hypotheses:
        history.record(history_key, hypotheses[0].category)

    return Diagnosis(tool_name=tool_name, hypotheses=tuple(hypotheses))


def _format_diagnosis(diagnosis: Diagnosis, *, single: bool) -> str:
    hyps = diagnosis.hypotheses[:1] if single else diagnosis.hypotheses
    if not hyps:
        return ""
    ranked = "; ".join(
        f"{h.rank}) {h.category.value} (p={h.confidence:g}, retry={'yes' if h.should_retry else 'no'})"
        for h in hyps
    )
    return f"\n\n[{DIAGNOSIS_GUIDANCE_PREFIX}: {ranked}. Weigh these before retrying.]"


def maybe_append_diagnosis(
    result: str | None,
    tool_name: str,
    *,
    failed: bool,
    mode: Any,
    consecutive_count: int = 0,
    max_hypotheses: int = 3,
    history: "HypothesisHistory | None" = None,
    history_key: str | None = None,
) -> str:
    """Runtime seam: append a ranked failure diagnosis to a failed result.

    Returns ``result`` **byte-for-byte unchanged** unless ``failed`` is true and
    ``mode`` is not ``off``. ``reflect`` appends only the single best hypothesis;
    ``multi-hypothesis`` appends the full ranked list. When ``history`` is passed
    (#1030), already-tried hypotheses are demoted so repeats surface fresh ones.
    Never raises.
    """
    text = result or ""
    resolved = DiagnosisMode.coerce(mode)
    if not failed or resolved is DiagnosisMode.off:
        return text
    try:
        diagnosis = diagnose_failure(
            tool_name,
            error=text[:2000],
            exit_code=None,
            consecutive_count=consecutive_count,
            max_hypotheses=max_hypotheses,
            history=history,
            history_key=history_key,
        )
    except Exception:
        return text
    return text + _format_diagnosis(diagnosis, single=resolved is DiagnosisMode.reflect)
