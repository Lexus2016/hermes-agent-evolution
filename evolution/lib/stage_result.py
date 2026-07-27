# -*- coding: utf-8 -*-
"""Structured result-tuple contract for evolution pipeline stages (issue #1338).

AREX-style ``StageResult`` — every pipeline stage emits this instead of a
free-form verdict, so downstream stages (and a future Accept/Refine/Restart
gate) receive a uniform, evidence-annotated payload.

This is **slice A** of the AREX confidence-gated loop (#1330): the contract
exists and is emitted at one boundary, but the consuming gate logic is deferred
to slice B (#1339).  Existing stage behaviour is unchanged — the tuple is purely
additive.

Design goals (matching the rest of the ``evolution/lib`` corpus):

* Pure Python, ``dataclasses`` — **no external dependencies**.
* Import-safe (no side effects on import), full type hints,
  ``from __future__ import annotations``.
* JSON serialisation via ``to_dict``/``from_dict``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = ["StageResult"]


@dataclass
class StageResult:
    """Uniform result contract emitted by every evolution pipeline stage.

    Attributes
    ----------
    result
        The stage's payload — typically the dict it would have written to disk
        anyway (e.g. the analysis JSON, the issues JSON).  Left as ``Any`` so
        each stage controls its own internal schema.
    evidence_pointers
        Paths / URLs the result rests on (sidecar files read, issues queried,
        upstream artefacts consumed).  Provides a traceable chain without
        requiring the consumer to re-derive the stage's inputs.
    confidence
        Self-assessed confidence on an integer 0–100 scale (0 = no data,
        100 = fully certain).  For slice A this is informational only — the
        gate that *acts* on confidence comes in slice B (#1339).
    stage
        Human-readable name of the emitting stage (e.g. ``"local_triage"``).
    timestamp
        ISO-8601 UTC string when the result was produced, or ``""`` if unset.
    """

    result: Any = None
    evidence_pointers: list[str] = field(default_factory=list)
    confidence: int = 0
    stage: str = ""
    timestamp: str = ""

    # -- serialisation ----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dict representation."""
        return {
            "result": self.result,
            "evidence_pointers": list(self.evidence_pointers),
            "confidence": self.confidence,
            "stage": self.stage,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StageResult:
        """Reconstruct from a :meth:`to_dict` payload."""
        return cls(
            result=data.get("result"),
            evidence_pointers=list(data.get("evidence_pointers", [])),
            confidence=int(data.get("confidence", 0)),
            stage=data.get("stage", ""),
            timestamp=data.get("timestamp", ""),
        )

    # -- helpers ----------------------------------------------------------

    @classmethod
    def wrap(
        cls,
        result: Any,
        evidence_pointers: list[str] | None = None,
        *,
        confidence: int = 0,
        stage: str = "",
        timestamp: str = "",
    ) -> StageResult:
        """Convenience factory used at stage boundaries.

        ``confidence`` is clamped to 0–100.  When ``confidence`` is left at 0
        but ``evidence_pointers`` is non-empty, a sensible default of 50 is
        used (``"we have data but haven't assessed how much to trust it"``).
        """
        ev = list(evidence_pointers or [])
        if confidence == 0 and ev:
            confidence = 50
        confidence = max(0, min(100, confidence))
        return cls(
            result=result,
            evidence_pointers=ev,
            confidence=confidence,
            stage=stage,
            timestamp=timestamp,
        )
