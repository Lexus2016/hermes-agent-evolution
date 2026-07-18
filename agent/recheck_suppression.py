# -*- coding: utf-8 -*-
"""Redundant self-verification recheck suppression (issue #1041, child of #1020).

Second slice of recheck suppression. The first slice
(``evolution/lib/recheck_classifier.py``) provided a standalone classifier; this
is the **production, importable** controller that actually *suppresses*
high-confidence redundant rechecks at the tool-call guardrail and logs every
decision for calibration.

A "recheck" here is a redundant self-verification: re-issuing an idempotent
read-only call (``read_file``, ``search_files``, …) that just succeeded, with no
intervening action that could have changed the answer. Suppressing it saves
tokens without losing information. The controller is deliberately conservative —
it only suppresses an **immediate identical repeat** of a successful idempotent
call, so it can never drop a verification that a real intervening change made
meaningful.

Design goals (matching ``agent/tool_guardrails.py``):

* Pure logic + frozen dataclasses; **no side effects on import**.
* Standard library only; full type hints; ``from __future__ import annotations``.
* Config built from a mapping (``from_mapping``) like ``ToolCallGuardrailConfig``.
* Every decision is logged to a bounded in-memory :class:`CalibrationLog`.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Deque, Mapping

__all__ = [
    "RecheckVerdict",
    "RecheckResult",
    "RecheckSuppressionConfig",
    "CalibrationLog",
    "RecheckClassifier",
    "RecheckController",
]


class RecheckVerdict(str, Enum):
    """Classification of a proposed verification call."""

    rethink = "rethink"  # legitimate — keep it
    recheck = "recheck"  # redundant — suppression candidate
    uncertain = "uncertain"  # not enough information to decide


@dataclass(frozen=True)
class RecheckResult:
    """Outcome of classifying one proposed call."""

    verdict: RecheckVerdict
    confidence: float
    reason: str = ""
    tool_name: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "confidence": self.confidence,
            "reason": self.reason,
            "tool_name": self.tool_name,
        }


@dataclass(frozen=True)
class RecheckSuppressionConfig:
    """Config for recheck suppression (``recheck_suppression`` config section)."""

    enabled: bool = False
    min_confidence: float = 0.85
    log_capacity: int = 200

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "RecheckSuppressionConfig":
        if not isinstance(data, Mapping):
            return cls()
        defaults = cls()
        enabled = data.get("enabled", defaults.enabled)
        try:
            min_conf = float(data.get("min_confidence", defaults.min_confidence))
        except (TypeError, ValueError):
            min_conf = defaults.min_confidence
        min_conf = min(1.0, max(0.0, min_conf))
        try:
            cap = int(data.get("log_capacity", defaults.log_capacity))
        except (TypeError, ValueError):
            cap = defaults.log_capacity
        return cls(enabled=bool(enabled), min_confidence=min_conf, log_capacity=max(1, cap))


@dataclass
class CalibrationLog:
    """Bounded in-memory record of suppression decisions for calibration."""

    capacity: int = 200
    _entries: Deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=200))

    def __post_init__(self) -> None:
        # Rebuild the deque with the requested capacity.
        self._entries = deque(self._entries, maxlen=max(1, self.capacity))

    def record(self, result: RecheckResult, *, suppressed: bool) -> None:
        entry = result.to_dict()
        entry["suppressed"] = suppressed
        self._entries.append(entry)

    def entries(self) -> list[dict[str, Any]]:
        return list(self._entries)

    @property
    def suppressed_count(self) -> int:
        return sum(1 for e in self._entries if e.get("suppressed"))

    def __len__(self) -> int:
        return len(self._entries)


# Confidence assigned to an immediate identical repeat of a successful idempotent
# call — the only pattern this conservative slice treats as a redundant recheck.
_IMMEDIATE_REPEAT_CONFIDENCE = 0.9


class RecheckClassifier:
    """Heuristic recheck classifier.

    Conservative by construction: only an *immediate identical repeat* of a
    successful idempotent call is a ``recheck``; everything else is ``rethink``.
    """

    def classify(
        self,
        tool_name: str,
        *,
        is_idempotent: bool,
        is_immediate_repeat: bool,
        prior_succeeded: bool,
    ) -> RecheckResult:
        if not is_idempotent:
            return RecheckResult(
                RecheckVerdict.rethink,
                0.0,
                "mutating tool — never suppressed",
                tool_name,
            )
        if is_immediate_repeat and prior_succeeded:
            return RecheckResult(
                RecheckVerdict.recheck,
                _IMMEDIATE_REPEAT_CONFIDENCE,
                "immediate identical repeat of a successful read-only call",
                tool_name,
            )
        return RecheckResult(
            RecheckVerdict.rethink,
            0.0,
            "not an immediate redundant repeat",
            tool_name,
        )


class RecheckController:
    """Owns the classifier, suppression policy, and calibration log."""

    def __init__(
        self,
        config: RecheckSuppressionConfig | None = None,
        classifier: RecheckClassifier | None = None,
        calibration_log: CalibrationLog | None = None,
    ) -> None:
        self.config = config or RecheckSuppressionConfig()
        self.classifier = classifier or RecheckClassifier()
        self.calibration_log = calibration_log or CalibrationLog(self.config.log_capacity)

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    def decide(
        self,
        tool_name: str,
        *,
        is_idempotent: bool,
        is_immediate_repeat: bool,
        prior_succeeded: bool,
    ) -> tuple[bool, RecheckResult]:
        """Classify a proposed call and decide whether to suppress it.

        Always logs the decision to the calibration log. Returns
        ``(suppress, result)``.
        """
        result = self.classifier.classify(
            tool_name,
            is_idempotent=is_idempotent,
            is_immediate_repeat=is_immediate_repeat,
            prior_succeeded=prior_succeeded,
        )
        suppress = (
            result.verdict is RecheckVerdict.recheck
            and result.confidence >= self.config.min_confidence
        )
        self.calibration_log.record(result, suppressed=suppress)
        return suppress, result

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "RecheckController":
        config = RecheckSuppressionConfig.from_mapping(data)
        return cls(config=config)
