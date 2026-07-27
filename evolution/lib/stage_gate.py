# -*- coding: utf-8 -*-
"""Confidence-gated Accept/Refine/Restart branch for stage boundaries (#1339).

Slice B of the AREX loop (#1330).  Slice A (#1338) gave every stage a uniform
:class:`~evolution.lib.stage_result.StageResult`; this module is the gate that
*acts* on the confidence carried in it.

Three branches, decided at a stage boundary:

* **Accept**  — ``confidence >= threshold``.  Proceed as normal.
* **Refine**  — below threshold but the trajectory is recoverable: keep the
  evidence already gathered and re-investigate only the gaps.
* **Restart** — below threshold and the trajectory is too noisy to salvage:
  discard it and reinitialize from the original problem.

On recoverability
-----------------
The issue specifies the recoverable-vs-noisy call is "produced by the model
running the stage".  A deterministic script has no model, so ``decide`` takes an
explicit ``recoverable`` argument for callers that *do* have a judgement, and
falls back to a conservative structural proxy when it is ``None``: a result with
evidence pointers has something concrete to build on and is refinable; a result
with none is indistinguishable from noise and restarts.

The threshold defaults to 70 — deliberately conservative, per the issue's
"conservative τ prevents runaway looping" criterion.  Note the interaction with
:meth:`StageResult.wrap`, which assigns 50 to any result that has evidence but
no self-assessed confidence: such a result lands in Refine, not Accept, so an
un-assessed stage is never silently trusted.

Pure Python, no external dependencies, no side effects on import — matching the
rest of ``evolution/lib``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from evolution.lib.stage_result import StageResult

__all__ = [
    "ACCEPT",
    "REFINE",
    "RESTART",
    "DEFAULT_CONFIDENCE_THRESHOLD",
    "GateDecision",
    "decide",
]

ACCEPT = "accept"
REFINE = "refine"
RESTART = "restart"

#: Conservative default τ.  See module docstring on the 50-confidence interaction.
DEFAULT_CONFIDENCE_THRESHOLD = 70


@dataclass
class GateDecision:
    """The branch taken at one stage boundary, with the reasoning that got there.

    Attributes
    ----------
    branch
        One of :data:`ACCEPT`, :data:`REFINE`, :data:`RESTART`.
    stage
        Name of the stage this decision was made for.
    confidence
        The confidence that was gated on.
    threshold
        The τ it was compared against.
    reason
        One line, human-readable — this is what gets logged.
    retained_evidence
        Evidence pointers carried forward.  Populated for Accept and Refine;
        empty for Restart, which by definition discards the trajectory.
    """

    branch: str
    stage: str
    confidence: int
    threshold: int
    reason: str
    retained_evidence: list[str]

    @property
    def proceeds(self) -> bool:
        """True when the pipeline may consume the result as-is (Accept only)."""
        return self.branch == ACCEPT

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dict representation."""
        return {
            "branch": self.branch,
            "stage": self.stage,
            "confidence": self.confidence,
            "threshold": self.threshold,
            "reason": self.reason,
            "retained_evidence": list(self.retained_evidence),
        }

    def log_line(self) -> str:
        """Single-line log record for the boundary (#1339 success criterion)."""
        return (
            f"[stage-gate] {self.stage}: {self.branch.upper()} "
            f"(confidence={self.confidence}, threshold={self.threshold}) — {self.reason}"
        )


def decide(
    stage_result: StageResult,
    *,
    threshold: int = DEFAULT_CONFIDENCE_THRESHOLD,
    recoverable: bool | None = None,
) -> GateDecision:
    """Choose Accept / Refine / Restart for a completed stage.

    Parameters
    ----------
    stage_result
        The tuple emitted at this boundary (#1338).
    threshold
        τ — confidence at or above which the result is accepted.  Clamped to
        0–100 to match ``StageResult``'s own clamping.
    recoverable
        Explicit recoverable-vs-noisy judgement from the model running the
        stage.  When ``None``, falls back to the structural proxy described in
        the module docstring (evidence present ⇒ recoverable).

    Returns
    -------
    GateDecision
        Always returned — the gate never raises, so a stage boundary cannot be
        taken down by its own instrumentation.
    """
    threshold = max(0, min(100, threshold))
    confidence = max(0, min(100, int(stage_result.confidence)))
    evidence = list(stage_result.evidence_pointers)
    stage = stage_result.stage or "unknown"

    if confidence >= threshold:
        return GateDecision(
            branch=ACCEPT,
            stage=stage,
            confidence=confidence,
            threshold=threshold,
            reason=f"confidence at or above threshold; {len(evidence)} evidence pointer(s)",
            retained_evidence=evidence,
        )

    is_recoverable = bool(evidence) if recoverable is None else bool(recoverable)

    if is_recoverable:
        return GateDecision(
            branch=REFINE,
            stage=stage,
            confidence=confidence,
            threshold=threshold,
            reason=(
                f"below threshold but recoverable — retaining {len(evidence)} "
                f"evidence pointer(s) and re-investigating the gaps"
            ),
            retained_evidence=evidence,
        )

    return GateDecision(
        branch=RESTART,
        stage=stage,
        confidence=confidence,
        threshold=threshold,
        reason=(
            "below threshold with no salvageable evidence — discarding the "
            "trajectory and reinitializing from the original problem"
        ),
        retained_evidence=[],
    )
