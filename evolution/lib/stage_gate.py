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

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evolution.lib.stage_result import StageResult

__all__ = [
    "ACCEPT",
    "REFINE",
    "RESTART",
    "DEFAULT_CONFIDENCE_THRESHOLD",
    "RESTART_RATE_ALERT_THRESHOLD",
    "GateDecision",
    "decide",
    "record_decision",
    "load_decisions",
    "compute_gate_rates",
    "gate_flags",
    "format_gate_rates",
]

ACCEPT = "accept"
REFINE = "refine"
RESTART = "restart"

#: Conservative default τ.  See module docstring on the 50-confidence interaction.
DEFAULT_CONFIDENCE_THRESHOLD = 70

#: A boundary restarting more than this share of the time is mis-tuned, not
#: merely unlucky — the τ is too high for the confidence that boundary can
#: realistically self-assess, or the stage genuinely has no evidence to work
#: with.  Either way it warrants investigation rather than silent looping
#: (#1340).
RESTART_RATE_ALERT_THRESHOLD = 0.25


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


# ── Observability: per-boundary branch rates (#1340, slice C) ───────────────
# A gate that nobody measures is a gate nobody can tune. decide() returns a
# decision; these persist it and turn a cycle's worth of decisions into the two
# rates the AREX loop is judged on, plus the alert flag.


def record_decision(ledger_file: Path, decision: GateDecision) -> None:
    """Append one gate decision to the JSONL ledger.

    Never raises: this is instrumentation attached to a live pipeline boundary,
    and losing a metrics line must not take the stage down with it.
    """
    try:
        ledger_file.parent.mkdir(parents=True, exist_ok=True)
        with open(ledger_file, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(decision.to_dict(), sort_keys=True) + "\n")
    except OSError:
        pass


def load_decisions(ledger_file: Path) -> list[dict[str, Any]]:
    """Read the decision ledger, skipping malformed lines."""
    if not ledger_file.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        lines = ledger_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        try:
            rec = json.loads(ln)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(rec, dict) and rec.get("branch") in (ACCEPT, REFINE, RESTART):
            out.append(rec)
    return out


def compute_gate_rates(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Aggregate decisions into per-boundary branch rates.

    Returns ``{stage: {total, accept, refine, restart, stage_refine_rate,
    stage_restart_rate}}``.  Rates are shares of that boundary's decisions, so
    a boundary that ran twice is not compared on equal footing with one that ran
    two hundred times — read ``total`` alongside the rate.
    """
    by_stage: dict[str, dict[str, Any]] = {}
    for rec in records:
        # Coerced to str so the key type is guaranteed homogeneous. A ledger
        # line carrying a non-string stage (a second writer, a hand-edited
        # line, a GateDecision built directly — the dataclass validates
        # nothing) would otherwise make `sorted(rates.items())` raise
        # TypeError in gate_flags. compute_health catches Exception broadly,
        # so that would silently drop the rates AND the alert, reporting
        # "healthy" while a real restart breach sat in the ledger — and the
        # ledger is append-only and never rotated, so one bad line would
        # blind the feature permanently.
        stage = str(rec.get("stage") or "unknown")
        bucket = by_stage.setdefault(
            stage, {"total": 0, ACCEPT: 0, REFINE: 0, RESTART: 0}
        )
        bucket["total"] += 1
        bucket[rec["branch"]] += 1
    for bucket in by_stage.values():
        total = bucket["total"] or 1
        bucket["stage_refine_rate"] = round(bucket[REFINE] / total, 4)
        bucket["stage_restart_rate"] = round(bucket[RESTART] / total, 4)
    return by_stage


def gate_flags(
    rates: dict[str, dict[str, Any]],
    *,
    restart_threshold: float = RESTART_RATE_ALERT_THRESHOLD,
    min_decisions: int = 4,
) -> list[str]:
    """Alert flags for mis-tuned boundaries (#1340).

    ``min_decisions`` guards against a single unlucky restart on a boundary that
    has only run once reading as a 100% restart rate.
    """
    flags: list[str] = []
    for stage, bucket in sorted(rates.items()):
        if bucket["total"] < min_decisions:
            continue
        if bucket["stage_restart_rate"] > restart_threshold:
            flags.append(
                f"HIGH_STAGE_RESTART_RATE:{stage}="
                f"{bucket['stage_restart_rate']:.0%}"
            )
    return flags


def format_gate_rates(rates: dict[str, dict[str, Any]]) -> str:
    """One-line summary per boundary for the evolution-health sidecar."""
    if not rates:
        return ""
    parts = [
        f"{stage}(n={b['total']} refine={b['stage_refine_rate']:.0%} "
        f"restart={b['stage_restart_rate']:.0%})"
        for stage, b in sorted(rates.items())
    ]
    return "[stage-gate] " + " ".join(parts)
