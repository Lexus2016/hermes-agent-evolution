# -*- coding: utf-8 -*-
"""Recheck Classifier: detect redundant self-verification steps.

This module implements GitHub issue #1038: *Suppress redundant self-verification
rechecks to reduce token waste* (child of #1020).

When an agent is about to initiate a verification step (e.g. re-reading a file
it just read, re-running a test it just ran), it often does so redundantly —
consuming tokens without gaining information.  This lightweight prompt-level
classifier examines the proposed verification call against recent tool history
and decides whether to keep the step (``RETHINK``) or flag it as a redundant
recheck candidate for suppression (``RECHECK``).

Components
----------
* :class:`RecheckVerdict` — enumeration of classification outcomes.
* :class:`RecheckContext` — the proposed call plus recent tool-call history.
* :class:`RecheckResult` — classification result with confidence and reason.
* :class:`RecheckClassifier` — heuristic classifier with recency / repetition
  factor computation and running statistics.
* :class:`RecheckSuppressionPolicy` — threshold-based suppression decision.

Design principles
-----------------
* Pure functions + dataclasses; **no side effects on import**.
* Full type hints with ``from __future__ import annotations``.
* JSON serialization (``to_dict`` / ``from_dict``) for all dataclasses.
* **Zero external dependencies** — standard library only.
* ``__version__`` and ``__all__`` for clean public surface.

GitHub issue: Lexus2016/hermes-agent-evolution #1038.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

__version__ = "1.0.0"

__all__ = [
    "RecheckVerdict",
    "RecheckContext",
    "RecheckResult",
    "RecheckClassifier",
    "RecheckSuppressionPolicy",
    "__version__",
]


# ---------------------------------------------------------------------------
# RecheckVerdict enum
# ---------------------------------------------------------------------------

class RecheckVerdict(Enum):
    """Classification outcome for a proposed verification step.

    * ``RETHINK``  — the step is legitimate; keep it.
    * ``RECHECK``  — the step looks redundant; suppress candidate.
    * ``UNCERTAIN``— insufficient information to decide.
    """

    RETHINK = "rethink"
    RECHECK = "recheck"
    UNCERTAIN = "uncertain"

    @classmethod
    def from_value(cls, value: Any) -> "RecheckVerdict":
        """Coerce a raw value (string or member) into a :class:`RecheckVerdict`.

        Accepts the enum member itself, its ``.value``, or an upper/lower
        copy of the value.  Raises ``ValueError`` for unknown inputs.
        """
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            for member in cls:
                if member.value == normalized or member.name.lower() == normalized:
                    return member
        raise ValueError(f"Unknown RecheckVerdict value: {value!r}")


# ---------------------------------------------------------------------------
# RecheckContext dataclass
# ---------------------------------------------------------------------------

@dataclass
class RecheckContext:
    """The proposed verification call plus recent tool-call history.

    Attributes
    ----------
    tool_name:
        Name of the tool that is about to be called (e.g. ``"read_file"``).
    action:
        The specific action / argument signature being verified (e.g.
        ``"read /app/main.py"``).  Free-form; used for exact-match comparison.
    recent_calls:
        Chronologically-ordered list of recent tool invocations.  Each entry
        is a dict with at least ``tool_name``, ``action``, and ``timestamp``
        keys.  ``timestamp`` should be a Unix epoch float.
    target_description:
        Optional human-readable description of what the verification targets.
    call_count:
        How many times *this exact* check (same ``tool_name`` + ``action``)
        has already been performed, excluding the proposed call.  When not
        provided the classifier infers it from ``recent_calls``.
    """

    tool_name: str = ""
    action: str = ""
    recent_calls: List[Dict[str, Any]] = field(default_factory=list)
    target_description: str = ""
    call_count: Optional[int] = None

    # -- computed helpers ---------------------------------------------------

    def resolved_call_count(self) -> int:
        """Return the effective ``call_count``, inferring from history if None."""
        if self.call_count is not None:
            return self.call_count
        return sum(
            1
            for c in self.recent_calls
            if c.get("tool_name") == self.tool_name
            and c.get("action") == self.action
        )

    def matching_calls(self) -> List[Dict[str, Any]]:
        """Return the subset of ``recent_calls`` that match tool+action."""
        return [
            c
            for c in self.recent_calls
            if c.get("tool_name") == self.tool_name
            and c.get("action") == self.action
        ]

    def last_matching_timestamp(self) -> Optional[float]:
        """Return the timestamp of the most recent matching call, if any."""
        matches = self.matching_calls()
        if not matches:
            return None
        ts = matches[-1].get("timestamp")
        return float(ts) if ts is not None else None

    def has_intervening_different_action(self) -> bool:
        """True if a non-matching call appears *after* the first matching call.

        If any different tool/action was invoked between matching calls (or
        after the last matching call), the proposed re-verification is
        considered justified — something may have changed.
        """
        seen_match = False
        for c in self.recent_calls:
            is_match = (
                c.get("tool_name") == self.tool_name
                and c.get("action") == self.action
            )
            if is_match:
                seen_match = True
            elif seen_match:
                return True
        return False

    # -- serialization ------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "action": self.action,
            "recent_calls": list(self.recent_calls),
            "target_description": self.target_description,
            "call_count": self.call_count,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RecheckContext":
        return cls(
            tool_name=data.get("tool_name", ""),
            action=data.get("action", ""),
            recent_calls=list(data.get("recent_calls", []) or []),
            target_description=data.get("target_description", ""),
            call_count=data.get("call_count"),
        )


# ---------------------------------------------------------------------------
# RecheckResult dataclass
# ---------------------------------------------------------------------------

@dataclass
class RecheckResult:
    """Classification result for a single proposed verification step."""

    verdict: RecheckVerdict = RecheckVerdict.UNCERTAIN
    confidence: float = 0.0
    reason: str = ""
    context: Optional[RecheckContext] = None

    # -- serialization ------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "confidence": self.confidence,
            "reason": self.reason,
            "context": self.context.to_dict() if self.context else None,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RecheckResult":
        ctx_data = data.get("context")
        return cls(
            verdict=RecheckVerdict.from_value(data.get("verdict", "uncertain")),
            confidence=float(data.get("confidence", 0.0)),
            reason=data.get("reason", ""),
            context=RecheckContext.from_dict(ctx_data) if ctx_data else None,
        )


# ---------------------------------------------------------------------------
# RecheckClassifier
# ---------------------------------------------------------------------------

class RecheckClassifier:
    """Heuristic classifier that flags redundant verification steps.

    Core logic
    ----------
    * If the same ``tool_name`` + ``action`` has been called **>= 2** times in
      ``recent_calls`` *and* there is **no intervening different action**,
      classify as ``RECHECK`` (redundant).
    * If it has been called exactly **once**, classify as ``RETHINK``
      (legitimate — a single prior call does not prove redundancy).
    * If there **is** an intervening different action, classify as ``RETHINK``
      because the state may have changed.
    * If there is **no history at all**, classify as ``UNCERTAIN``.

    Confidence is a blend of a *recency factor* (how recently the identical
    call was made) and a *repetition factor* (how many identical calls exist).
    """

    #: Window (seconds) within which an identical call is considered "recent".
    DEFAULT_RECENCY_WINDOW: float = 120.0

    def __init__(self, recency_window: Optional[float] = None) -> None:
        self.recency_window = (
            recency_window
            if recency_window is not None
            else self.DEFAULT_RECENCY_WINDOW
        )
        self._total = 0
        self._recheck = 0
        self._rethink = 0

    # -- public API ---------------------------------------------------------

    def classify(self, context: RecheckContext) -> RecheckResult:
        """Classify ``context`` and return a :class:`RecheckResult`."""
        self._total += 1

        # --- edge case: no tool information at all -------------------------
        if not context.tool_name and not context.action:
            result = RecheckResult(
                verdict=RecheckVerdict.UNCERTAIN,
                confidence=0.0,
                reason="No tool_name or action provided; cannot classify.",
                context=context,
            )
            return result

        matches = context.matching_calls()
        count = context.resolved_call_count()
        intervening = context.has_intervening_different_action()
        recency = self._compute_recency_factor(context)
        repetition = self._compute_repetition_factor(context)

        # --- UNCERTAIN: no history -----------------------------------------
        if not context.recent_calls:
            result = RecheckResult(
                verdict=RecheckVerdict.UNCERTAIN,
                confidence=0.1,
                reason="No recent call history available.",
                context=context,
            )
            return result

        # --- RECHECK: >= 2 identical calls, no intervening action ----------
        if count >= 2 and not intervening:
            confidence = min(1.0, 0.45 * repetition + 0.45 * recency + 0.1)
            reason = (
                f"Same tool+action '{context.tool_name}:{context.action}' "
                f"called {count} times with no intervening different action."
            )
            self._recheck += 1
            return RecheckResult(
                verdict=RecheckVerdict.RECHECK,
                confidence=round(confidence, 4),
                reason=reason,
                context=context,
            )

        # --- RETHINK: intervening different action makes re-check valid ----
        if intervening:
            confidence = min(1.0, 0.5 + 0.3 * (1.0 - recency))
            reason = (
                f"Different action observed between matching calls "
                f"for '{context.tool_name}:{context.action}'; re-verification "
                f"is justified."
            )
            self._rethink += 1
            return RecheckResult(
                verdict=RecheckVerdict.RETHINK,
                confidence=round(confidence, 4),
                reason=reason,
                context=context,
            )

        # --- RETHINK: only one prior call ----------------------------------
        if count <= 1:
            confidence = max(0.0, min(1.0, 0.6 - 0.2 * recency))
            reason = (
                f"Only {count} prior call(s) for "
                f"'{context.tool_name}:{context.action}'; "
                f"verification appears legitimate."
            )
            self._rethink += 1
            return RecheckResult(
                verdict=RecheckVerdict.RETHINK,
                confidence=round(confidence, 4),
                reason=reason,
                context=context,
            )

        # --- fallback ------------------------------------------------------
        self._rethink += 1
        return RecheckResult(
            verdict=RecheckVerdict.RETHINK,
            confidence=0.3,
            reason="Defaulting to RETHINK; heuristics inconclusive.",
            context=context,
        )

    # -- heuristic helpers --------------------------------------------------

    def _compute_recency_factor(self, context: RecheckContext) -> float:
        """Return a factor in ``[0, 1]`` for how recent the last matching call was.

        ``1.0`` means the call happened just now (``delta == 0``); ``0.0`` means
        it was ``>= recency_window`` seconds ago (or there is no prior call).
        """
        last_ts = context.last_matching_timestamp()
        if last_ts is None:
            return 0.0
        now = time.time()
        delta = now - last_ts
        if delta <= 0:
            return 1.0
        if delta >= self.recency_window:
            return 0.0
        return 1.0 - (delta / self.recency_window)

    @staticmethod
    def _compute_repetition_factor(context: RecheckContext) -> float:
        """Return a factor in ``[0, 1]`` for how many identical calls exist.

        Uses ``min(1.0, count / 4)`` so 4+ identical calls saturate the factor.
        """
        count = context.resolved_call_count()
        if count <= 0:
            return 0.0
        return min(1.0, count / 4.0)

    # -- statistics ---------------------------------------------------------

    def get_stats(self) -> Dict[str, int]:
        """Return cumulative classification counts.

        Keys: ``total``, ``recheck``, ``rethink``, ``uncertain``
        (uncertain is derived: ``total - recheck - rethink``).
        """
        return {
            "total": self._total,
            "recheck": self._recheck,
            "rethink": self._rethink,
            "uncertain": self._total - self._recheck - self._rethink,
        }

    def reset_stats(self) -> None:
        """Zero all accumulated statistics."""
        self._total = 0
        self._recheck = 0
        self._rethink = 0


# ---------------------------------------------------------------------------
# RecheckSuppressionPolicy
# ---------------------------------------------------------------------------

class RecheckSuppressionPolicy:
    """Decide whether a :class:`RecheckResult` should be acted upon.

    A result is suppressed (``True``) when the verdict is ``RECHECK`` **and**
    its confidence meets or exceeds the configured ``threshold``.
    """

    def __init__(self, threshold: float = 0.7) -> None:
        self.threshold = threshold

    def should_suppress(self, result: RecheckResult, threshold: Optional[float] = None) -> bool:
        """Return ``True`` if the result warrants suppression."""
        effective = threshold if threshold is not None else self.threshold
        return result.verdict is RecheckVerdict.RECHECK and result.confidence >= effective

    def get_suppression_reason(self, result: RecheckResult) -> str:
        """Return a human-readable explanation of the suppression decision."""
        if result.verdict is RecheckVerdict.RECHECK:
            if result.confidence >= self.threshold:
                return (
                    f"Suppressing redundant verification: "
                    f"confidence {result.confidence:.2f} >= threshold "
                    f"{self.threshold:.2f}. {result.reason}"
                )
            return (
                f"Redundant verification detected but confidence "
                f"{result.confidence:.2f} < threshold {self.threshold:.2f}; "
                f"allowing call to proceed."
            )
        if result.verdict is RecheckVerdict.RETHINK:
            return "Verification is legitimate (RETHINK); no suppression."
        return "Insufficient information (UNCERTAIN); no suppression."
