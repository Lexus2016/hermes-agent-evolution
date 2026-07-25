#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Evolving-memory pipeline state with broadcast final-outcome reward (issue #1269).

Pilots the two transferable architectural patterns from AgentFlow + Flow-GRPO
(arXiv:2510.05592, Stanford/Texas A&M/UC San Diego/Lambda) WITHOUT RL
infrastructure (Hermes has no training infra):

1. **Evolving memory M** — an explicit, deterministic, structured record of the
   reasoning process per pipeline stage (sub-goal, tool calls, result,
   verification status, turn index) — not latent thoughts.  Each stage is
   explicitly marked sufficient/insufficient, giving the pipeline a per-step
   "are we done?" signal it currently lacks.  This is a stronger design than
   the current ``metrics.jsonl`` (counts only) or the SkillHone decision
   history (#1256).

2. **Broadcast final-outcome reward to every stage** — Flow-GRPO's core trick:
   a single trajectory-level success signal assigned to every turn/stage.  At
   cycle end, the merge-success / metric-improvement signal is written back to
   every stage record from that cycle, giving a labeled dataset
   {stage decisions → cycle outcome} without per-stage reward shaping (which
   the paper shows is brittle).

3. **Offline correlation analysis (NOT imitation)** — periodically analyse
   which stage-level decisions correlate with successful cycles.  This is
   explicitly NOT imitation: the paper's critical caution is that offline SFT
   on GPT-4o trajectories causes catastrophic collapse (−19%) — token-level
   imitation misaligns with trajectory-level success.  The adaptation
   (in-the-flow) is the part that matters; this module only *identifies
   correlations*, it does not distil them.

4. **Pair with the adversarial-floor-test gate** — in-the-flow optimisation is
   only safe with an adversarially-robust reward (the BenchJack finding
   #1267).  The broadcast-reward signal must pass the null-agent floor test
   before it is trusted for correlation analysis.  This module imports the
   floor-test result as a gate on ``correlation_analysis``.

Design: pure, deterministic, standard-library only, no side effects on import.
Records are emitted to ``pipeline-traces.jsonl`` by the caller; this module
owns the schema, the broadcast-reward write-back, and the offline correlation
analysis.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

__all__ = [
    "VerificationStatus",
    "StageRecord",
    "CycleOutcome",
    "CorrelationFinding",
    "PipelineTracesReport",
    "make_stage_record",
    "broadcast_reward",
    "correlation_analysis",
    "evaluate",
    "main",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Verification status (the per-step "are we done?" signal) ────────────────


class VerificationStatus:
    """The evolving-memory verification flag from AgentFlow's Execution Verifier.

    ``sufficient`` — the stage's accumulated memory is enough to proceed / the
    stage is done.  ``insufficient`` — more work needed; the stage's output is
    not yet usable.  This is the per-step done signal the pipeline currently
    lacks (metrics.jsonl records only cycle-level counts).
    """

    SUFFICIENT = "sufficient"
    INSUFFICIENT = "insufficient"

    _VALID = frozenset({SUFFICIENT, INSUFFICIENT})

    @classmethod
    def validate(cls, status: str) -> str:
        if status not in cls._VALID:
            raise ValueError(
                f"verification_status must be one of {cls._VALID}, got {status!r}"
            )
        return status


# ── Evolving-memory stage record (the structured per-stage record) ──────────

# The pipeline stages, in order.  Each stage emits one or more StageRecords.
PIPELINE_STAGES: tuple[str, ...] = (
    "research",
    "issues",
    "analysis",
    "implementation",
    "metrics",
)


@dataclass
class StageRecord:
    """A single evolving-memory record for one pipeline stage at one turn.

    This is the structured, deterministic record AgentFlow's memory M holds per
    turn: sub-goal, tool calls, result, verification status, turn index.  Not
    latent thoughts — an explicit record the pipeline can analyse offline.
    """

    cycle_id: str
    stage: str
    turn_index: int
    sub_goal: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    result_summary: str = ""
    verification_status: str = VerificationStatus.INSUFFICIENT
    timestamp: str = ""
    # Broadcast-reward fields — written back at cycle end, not at stage time.
    final_outcome_reward: float | None = (
        None  # 0.0 (cycle failed) … 1.0 (cycle succeeded)
    )
    cycle_succeeded: bool | None = None

    def __post_init__(self) -> None:
        VerificationStatus.validate(self.verification_status)
        if not self.timestamp:
            self.timestamp = _now()

    def to_dict(self) -> dict[str, Any]:
        return {
            "cycle_id": self.cycle_id,
            "stage": self.stage,
            "turn_index": self.turn_index,
            "sub_goal": self.sub_goal,
            "tool_calls": list(self.tool_calls),
            "result_summary": self.result_summary,
            "verification_status": self.verification_status,
            "timestamp": self.timestamp,
            "final_outcome_reward": (
                round(self.final_outcome_reward, 6)
                if self.final_outcome_reward is not None
                else None
            ),
            "cycle_succeeded": self.cycle_succeeded,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "StageRecord":
        return cls(
            cycle_id=str(d["cycle_id"]),
            stage=str(d["stage"]),
            turn_index=int(d.get("turn_index", 0)),
            sub_goal=str(d.get("sub_goal", "")),
            tool_calls=list(d.get("tool_calls", [])),
            result_summary=str(d.get("result_summary", "")),
            verification_status=str(
                d.get("verification_status", VerificationStatus.INSUFFICIENT)
            ),
            timestamp=str(d.get("timestamp", "")),
            final_outcome_reward=(
                float(d["final_outcome_reward"])
                if d.get("final_outcome_reward") is not None
                else None
            ),
            cycle_succeeded=(
                bool(d["cycle_succeeded"])
                if d.get("cycle_succeeded") is not None
                else None
            ),
        )


def make_stage_record(
    cycle_id: str,
    stage: str,
    *,
    turn_index: int = 0,
    sub_goal: str = "",
    tool_calls: Sequence[Mapping[str, Any]] | None = None,
    result_summary: str = "",
    verification_status: str = VerificationStatus.INSUFFICIENT,
) -> StageRecord:
    """Construct a stage record (the caller appends it to pipeline-traces.jsonl)."""
    return StageRecord(
        cycle_id=cycle_id,
        stage=stage,
        turn_index=turn_index,
        sub_goal=sub_goal,
        tool_calls=[dict(tc) for tc in (tool_calls or [])],
        result_summary=result_summary,
        verification_status=verification_status,
    )


# ── Broadcast final-outcome reward ──────────────────────────────────────────


@dataclass(frozen=True)
class CycleOutcome:
    """The trajectory-level final-outcome signal for one cycle.

    ``reward`` is the single trajectory-level success signal that Flow-GRPO
    broadcasts to every turn/stage.  ``succeeded`` is the boolean form.  The
    adversarial-floor-test gate (``floor_test_passed``) must be True before the
    reward is trusted for correlation analysis — in-the-flow optimisation is
    only safe with an adversarially-robust reward (BenchJack #1267).
    """

    cycle_id: str
    succeeded: bool
    reward: float  # 0.0 … 1.0
    floor_test_passed: bool = True  # gate from #1267; default True for standalone use

    def to_dict(self) -> dict[str, Any]:
        return {
            "cycle_id": self.cycle_id,
            "succeeded": self.succeeded,
            "reward": round(self.reward, 6),
            "floor_test_passed": self.floor_test_passed,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "CycleOutcome":
        return cls(
            cycle_id=str(d["cycle_id"]),
            succeeded=bool(d["succeeded"]),
            reward=float(d.get("reward", 1.0 if d.get("succeeded") else 0.0)),
            floor_test_passed=bool(d.get("floor_test_passed", True)),
        )


def broadcast_reward(
    records: Sequence[StageRecord],
    outcome: CycleOutcome,
) -> list[StageRecord]:
    """Write the final-outcome reward back to every stage record from that cycle.

    This is Flow-GRPO's core trick: a single trajectory-level success signal
    assigned to every turn/stage.  Only records whose ``cycle_id`` matches
    ``outcome.cycle_id`` are updated.  Returns new StageRecord instances (the
    inputs are not mutated) so the caller can persist the updated set.
    """
    updated: list[StageRecord] = []
    for rec in records:
        if rec.cycle_id != outcome.cycle_id:
            updated.append(rec)
            continue
        updated.append(
            StageRecord(
                cycle_id=rec.cycle_id,
                stage=rec.stage,
                turn_index=rec.turn_index,
                sub_goal=rec.sub_goal,
                tool_calls=list(rec.tool_calls),
                result_summary=rec.result_summary,
                verification_status=rec.verification_status,
                timestamp=rec.timestamp,
                final_outcome_reward=outcome.reward,
                cycle_succeeded=outcome.succeeded,
            )
        )
    return updated


# ── Offline correlation analysis (NOT imitation) ────────────────────────────


@dataclass(frozen=True)
class CorrelationFinding:
    """A stage-level decision pattern correlated with cycle success.

    This is the output of the offline correlation analysis — it *identifies*
    which stage decisions predict success, it does NOT distil them into a
    policy (the −19% SFT-collapse caution: imitation misaligns with
    trajectory-level success).
    """

    stage: str
    decision_pattern: str
    success_rate_when_present: float
    success_rate_when_absent: float
    lift: float  # success_rate_when_present - success_rate_when_absent
    sample_cycles_present: int
    sample_cycles_absent: int
    note: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "decision_pattern": self.decision_pattern,
            "success_rate_when_present": round(self.success_rate_when_present, 6),
            "success_rate_when_absent": round(self.success_rate_when_absent, 6),
            "lift": round(self.lift, 6),
            "sample_cycles_present": self.sample_cycles_present,
            "sample_cycles_absent": self.sample_cycles_absent,
            "note": self.note,
        }


def _cycles_by_id(records: Sequence[StageRecord]) -> dict[str, list[StageRecord]]:
    by_cycle: dict[str, list[StageRecord]] = {}
    for rec in records:
        by_cycle.setdefault(rec.cycle_id, []).append(rec)
    return by_cycle


def _stage_success_rates(
    records: Sequence[StageRecord],
    stage: str,
    predicate,
) -> tuple[float, int, float, int]:
    """Compute (success_rate_when_predicate_holds, n_present, success_rate_when_not, n_absent)."""
    by_cycle = _cycles_by_id(records)
    present_success = 0
    present_total = 0
    absent_success = 0
    absent_total = 0
    for cid, recs in by_cycle.items():
        stage_recs = [r for r in recs if r.stage == stage]
        if not stage_recs:
            continue
        holds = any(predicate(r) for r in stage_recs)
        succeeded = any(
            r.cycle_succeeded for r in stage_recs if r.cycle_succeeded is not None
        )
        if holds:
            present_total += 1
            present_success += int(succeeded)
        else:
            absent_total += 1
            absent_success += int(succeeded)
    pr = present_success / present_total if present_total else 0.0
    ar = absent_success / absent_total if absent_total else 0.0
    return pr, present_total, ar, absent_total


def correlation_analysis(
    records: Sequence[StageRecord],
    *,
    floor_test_passed: bool = True,
    min_sample: int = 2,
) -> dict[str, Any]:
    """Offline correlation analysis: which stage decisions predict cycle success?

    **Gate**: if ``floor_test_passed`` is False, the analysis is refused — the
    broadcast-reward signal must pass the null-agent adversarial floor test
    (#1267) before it is trusted for correlation analysis.  In-the-flow
    optimisation is only safe with an adversarially-robust reward.

    **NOT imitation**: this function only *identifies* correlations; it does
    not produce a policy or distil trajectories.  The −19% SFT-collapse caution
    from the paper means the adaptation (in-the-flow) is the part that matters,
    not token-level imitation of past good cycles.

    The analysis examines a set of deterministic decision patterns per stage
    (e.g. "research stage marked sufficient", "analysis selected ≥3 issues",
    "implementation stage had >5 tool calls") and reports the lift in cycle
    success rate when each pattern holds vs. when it does not.
    """
    if not floor_test_passed:
        return {
            "refused": True,
            "reason": (
                "adversarial floor test did not pass — the broadcast-reward "
                "signal is not trusted for correlation analysis until #1267's "
                "null-agent floor test confirms the reward is not gameable"
            ),
            "findings": [],
        }

    # Decision patterns per stage.  Each is (pattern_name, predicate).
    # Predicates operate on a StageRecord and return bool.
    patterns: list[tuple[str, str, Any]] = [
        (
            "research",
            "research_marked_sufficient",
            lambda r: (
                r.stage == "research"
                and r.verification_status == VerificationStatus.SUFFICIENT
            ),
        ),
        (
            "research",
            "research_found_sources",
            lambda r: r.stage == "research" and len(r.tool_calls) >= 2,
        ),
        (
            "issues",
            "issues_filed",
            lambda r: (
                r.stage == "issues"
                and r.verification_status == VerificationStatus.SUFFICIENT
            ),
        ),
        (
            "analysis",
            "analysis_selected_multiple",
            lambda r: (
                r.stage == "analysis"
                and len(r.tool_calls) >= 1
                and r.verification_status == VerificationStatus.SUFFICIENT
            ),
        ),
        (
            "implementation",
            "implementation_marked_sufficient",
            lambda r: (
                r.stage == "implementation"
                and r.verification_status == VerificationStatus.SUFFICIENT
            ),
        ),
        (
            "implementation",
            "implementation_high_tool_count",
            lambda r: r.stage == "implementation" and len(r.tool_calls) >= 5,
        ),
        (
            "metrics",
            "metrics_recorded",
            lambda r: (
                r.stage == "metrics"
                and r.verification_status == VerificationStatus.SUFFICIENT
            ),
        ),
    ]

    findings: list[CorrelationFinding] = []
    for stage, pattern_name, pred in patterns:
        pr, n_present, ar, n_absent = _stage_success_rates(records, stage, pred)
        if n_present < min_sample and n_absent < min_sample:
            continue
        findings.append(
            CorrelationFinding(
                stage=stage,
                decision_pattern=pattern_name,
                success_rate_when_present=pr,
                success_rate_when_absent=ar,
                lift=pr - ar,
                sample_cycles_present=n_present,
                sample_cycles_absent=n_absent,
                note=(
                    "correlation identified, NOT distilled — the −19% SFT-collapse "
                    "caution means imitation misaligns with trajectory-level success; "
                    "use this signal to adjust triage heuristics, not to imitate cycles"
                ),
            )
        )

    # Sort by lift descending — the most predictive patterns first.
    findings.sort(key=lambda f: f.lift, reverse=True)

    return {
        "refused": False,
        "findings": [f.to_dict() for f in findings],
        "top_correlation": findings[0].to_dict() if findings else None,
        "note": (
            "offline correlation analysis only — identifies which stage "
            "decisions predict success; does NOT distil into a policy (respects "
            "the AgentFlow −19% SFT-collapse caution against trajectory imitation)"
        ),
    }


# ── Aggregate report ────────────────────────────────────────────────────────


@dataclass
class PipelineTracesReport:
    records: list[StageRecord] = field(default_factory=list)
    cycle_outcome: CycleOutcome | None = None
    correlation: dict[str, Any] = field(default_factory=dict)

    @property
    def stages_emitting(self) -> list[str]:
        return sorted({r.stage for r in self.records})

    @property
    def cycles(self) -> list[str]:
        return sorted({r.cycle_id for r in self.records})

    @property
    def broadcast_complete(self) -> bool:
        if self.cycle_outcome is None:
            return False
        cycle_recs = [
            r for r in self.records if r.cycle_id == self.cycle_outcome.cycle_id
        ]
        return all(r.final_outcome_reward is not None for r in cycle_recs)

    def to_dict(self) -> dict[str, Any]:
        return {
            "records": [r.to_dict() for r in self.records],
            "cycle_outcome": self.cycle_outcome.to_dict()
            if self.cycle_outcome
            else None,
            "correlation": self.correlation,
            "summary": {
                "stages_emitting": self.stages_emitting,
                "cycles": self.cycles,
                "broadcast_complete": self.broadcast_complete,
            },
        }


def evaluate(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Core entry from a JSON payload.

    Expected payload shape::

        {
          "records": [{"cycle_id": "2026-07-25", "stage": "research",
                        "turn_index": 0, "verification_status": "sufficient", ...}, ...],
          "cycle_outcome": {"cycle_id": "2026-07-25", "succeeded": true,
                            "reward": 1.0, "floor_test_passed": true},
          "correlation_min_sample": 2
        }
    """
    records = [StageRecord.from_dict(r) for r in payload.get("records", [])]

    report = PipelineTracesReport(records=records)

    outcome_raw = payload.get("cycle_outcome")
    if outcome_raw:
        outcome = CycleOutcome.from_dict(outcome_raw)
        report.cycle_outcome = outcome
        # Broadcast the final-outcome reward to every record from this cycle.
        report.records = broadcast_reward(records, outcome)
        # Run the offline correlation analysis, gated on the floor test.
        report.correlation = correlation_analysis(
            report.records,
            floor_test_passed=outcome.floor_test_passed,
            min_sample=int(payload.get("correlation_min_sample", 2)),
        )

    return report.to_dict()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Evolving-memory pipeline state with broadcast final-outcome reward (#1269)",
    )
    parser.add_argument(
        "--payload",
        required=True,
        help="path to a JSON payload with stage records and cycle outcome",
    )
    args = parser.parse_args(argv)
    with open(args.payload, encoding="utf-8") as fh:
        payload = json.load(fh)
    report = evaluate(payload)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
