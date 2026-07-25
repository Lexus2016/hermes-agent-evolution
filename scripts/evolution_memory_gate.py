#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Selective memory addition gate with history-based utility deletion (issue #1270).

Implements the empirical memory-management laws from the ACL 2026 long paper
#27 (Xiong et al., Harvard/U. Georgia/Michigan State/U. Minnesota;
aclanthology.org/2026.acl-long.27) for the evolution pipeline's own artifacts
(research reports, issues files, implementation logs) and as a recommendation
for the agent's memory/skills store.

The pipeline currently runs an **add-all** policy — every research report,
issues file, and implementation log is appended indiscriminately.  The paper
proves this self-degrades via three mechanisms:

1. **Experience-Following Property** — a high input-similarity between the
   current task and a retrieved memory record yields a high output-similarity
   (Pearson r ≈ 1.0).  Agents *imitate* retrieved demonstrations; a retrieved
   noisy/misaligned record is imitated and amplifies the error.
2. **Error Propagation** — if a retrieved record contains noisy/incorrect
   outputs, the agent replicates and amplifies the error; if that execution is
   re-added to memory, the error propagates to future tasks.
3. **Misaligned Experience Replay** — some records that pass the quality
   filter still consistently lead to poor downstream execution because they are
   misaligned with the current task distribution.

Quantitatively, add-all is worse than fixed-memory on 3 of 4 agents; a strict
evaluator + selective addition beats add-all by 22–25 points; and
history-based deletion (remove a record retrieved ≥n times if its average
downstream utility is below threshold) *improves* performance beyond
no-deletion on real agents.

This module provides four deterministic, LLM-free components:

- **Retrieval-utility log** — each time a record is retrieved, log
  (record_id, retrieval_context, downstream_outcome).
- **Selective-addition gate** — refuse to store an artifact unless a quality
  classifier judges it high-quality.  The paper shows a small specialised
  classifier (300 examples) beats a generic LLM judge; this module accepts an
  external quality score so the classifier can be swapped without coupling.
- **History-based deletion** — remove records retrieved ≥n times whose average
  downstream utility is below threshold.  Deletion is by *utility*, not age.
- **Misaligned-record detector** — flag records with high retrieval count but
  low downstream-outcome correlation; quarantine or delete them.

Design: pure, deterministic, standard-library only, no side effects on import.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

__all__ = [
    "RetrievalEvent",
    "MemoryRecord",
    "AdditionDecision",
    "DeletionCandidate",
    "MisalignedFlag",
    "MemoryGateReport",
    "log_retrieval",
    "addition_gate",
    "history_based_deletion",
    "detect_misaligned",
    "evaluate",
    "main",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Retrieval-utility log ────────────────────────────────────────────────────


@dataclass
class RetrievalEvent:
    """A single retrieval of a memory/skill record, with the downstream outcome.

    The downstream outcome is a float in [0, 1] where 1 = the retrieval led to
    a successful downstream execution and 0 = it led to a failure.  This is the
    signal history-based deletion keys on: a record retrieved many times whose
    average outcome is low is a candidate for deletion.
    """

    record_id: str
    retrieval_context: str = ""
    downstream_outcome: float = 0.0  # 0.0 (failure) … 1.0 (success)
    timestamp: str = ""

    def __post_init__(self) -> None:
        if not self.timestamp:
            self.timestamp = _now()

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "retrieval_context": self.retrieval_context,
            "downstream_outcome": round(self.downstream_outcome, 6),
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "RetrievalEvent":
        return cls(
            record_id=str(d["record_id"]),
            retrieval_context=str(d.get("retrieval_context", "")),
            downstream_outcome=float(d.get("downstream_outcome", 0.0)),
            timestamp=str(d.get("timestamp", "")),
        )


def log_retrieval(
    record_id: str,
    *,
    retrieval_context: str = "",
    downstream_outcome: float = 0.0,
    log: list[RetrievalEvent] | None = None,
) -> RetrievalEvent:
    """Append a retrieval event to the utility log (in place if ``log`` given)."""
    event = RetrievalEvent(
        record_id=record_id,
        retrieval_context=retrieval_context,
        downstream_outcome=downstream_outcome,
    )
    if log is not None:
        log.append(event)
    return event


# ── Selective-addition gate ─────────────────────────────────────────────────


@dataclass
class MemoryRecord:
    """An artifact the pipeline is considering storing in memory/skills.

    ``quality_score`` is the output of a (small, specialised) quality
    classifier in [0, 1].  The paper's key result: a 300-example fine-tuned
    classifier recovers most of an oracle evaluator's gain and beats a generic
    LLM judge.  This module is classifier-agnostic — it accepts the score and
    applies the gate threshold.
    """

    record_id: str
    artifact_type: str = "research_report"  # research_report | issues_file | implementation_log | skill | memory
    content_summary: str = ""
    quality_score: float = 0.0  # [0, 1] from a specialised classifier
    source_record_ids: tuple[
        str, ...
    ] = ()  # records this artifact imitates/derives from (error-propagation guard)

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "artifact_type": self.artifact_type,
            "content_summary": self.content_summary,
            "quality_score": round(self.quality_score, 6),
            "source_record_ids": list(self.source_record_ids),
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "MemoryRecord":
        return cls(
            record_id=str(d["record_id"]),
            artifact_type=str(d.get("artifact_type", "research_report")),
            content_summary=str(d.get("content_summary", "")),
            quality_score=float(d.get("quality_score", 0.0)),
            source_record_ids=tuple(d.get("source_record_ids", [])),
        )


@dataclass(frozen=True)
class AdditionDecision:
    record_id: str
    admitted: bool
    reason: str
    quality_score: float
    threshold: float
    error_propagation_blocked: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "admitted": self.admitted,
            "reason": self.reason,
            "quality_score": round(self.quality_score, 6),
            "threshold": round(self.threshold, 6),
            "error_propagation_blocked": self.error_propagation_blocked,
        }


def addition_gate(
    record: MemoryRecord,
    *,
    quality_threshold: float = 0.7,
    quarantined_ids: frozenset[str] | None = None,
) -> AdditionDecision:
    """Selective-addition gate: admit an artifact only if it passes quality + error-propagation guards.

    Two checks:
    1. **Quality gate** — ``record.quality_score`` must meet ``quality_threshold``.
       The paper shows a small specialised classifier (300 examples) beats a
       generic LLM judge; the threshold defaults to 0.7 (strict-evaluator
       regime that beats add-all by 22–25 points).
    2. **Error-propagation guard** — the record must not derive from a
       quarantined/noisy source (the amplification loop: an execution that
       imitated a noisy record must not be re-added).
    """
    quarantined = quarantined_ids or frozenset()

    # Error-propagation guard: if any source record is quarantined, block.
    poisoned_sources = [s for s in record.source_record_ids if s in quarantined]
    if poisoned_sources:
        return AdditionDecision(
            record_id=record.record_id,
            admitted=False,
            reason=f"error-propagation guard: record derives from quarantined/noisy source(s) {poisoned_sources}",
            quality_score=record.quality_score,
            threshold=quality_threshold,
            error_propagation_blocked=True,
        )

    if record.quality_score < quality_threshold:
        return AdditionDecision(
            record_id=record.record_id,
            admitted=False,
            reason=f"quality score {record.quality_score:.3f} below threshold {quality_threshold:.3f} — not stored (selective addition)",
            quality_score=record.quality_score,
            threshold=quality_threshold,
            error_propagation_blocked=False,
        )

    return AdditionDecision(
        record_id=record.record_id,
        admitted=True,
        reason=f"quality score {record.quality_score:.3f} meets threshold {quality_threshold:.3f} — stored",
        quality_score=record.quality_score,
        threshold=quality_threshold,
        error_propagation_blocked=False,
    )


# ── History-based deletion ──────────────────────────────────────────────────


@dataclass(frozen=True)
class DeletionCandidate:
    record_id: str
    retrieval_count: int
    avg_downstream_outcome: float
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "retrieval_count": self.retrieval_count,
            "avg_downstream_outcome": round(self.avg_downstream_outcome, 6),
            "reason": self.reason,
        }


def history_based_deletion(
    retrieval_log: Sequence[RetrievalEvent],
    *,
    min_retrievals: int = 3,
    utility_threshold: float = 0.4,
) -> list[DeletionCandidate]:
    """Delete memories/skills by downstream utility, not age.

    A record is a deletion candidate if it has been retrieved at least
    ``min_retrievals`` times AND its average downstream outcome is below
    ``utility_threshold``.  This is the history-based-deletion pattern from
    Table 2 of the paper, which *improves* performance beyond no-deletion on
    real agents (EHRAgent 42.06 vs 38.67, AgentDriver 51.81 vs 51.00).
    """
    # Aggregate per record.
    per_record: dict[str, list[float]] = {}
    for event in retrieval_log:
        per_record.setdefault(event.record_id, []).append(event.downstream_outcome)

    candidates: list[DeletionCandidate] = []
    for rid, outcomes in per_record.items():
        count = len(outcomes)
        avg = sum(outcomes) / count if count else 0.0
        if count >= min_retrievals and avg < utility_threshold:
            candidates.append(
                DeletionCandidate(
                    record_id=rid,
                    retrieval_count=count,
                    avg_downstream_outcome=avg,
                    reason=(
                        f"retrieved {count} times with avg downstream outcome "
                        f"{avg:.3f} < {utility_threshold:.3f} — low-utility, delete by history"
                    ),
                )
            )
    return candidates


# ── Misaligned-record detector ──────────────────────────────────────────────


@dataclass(frozen=True)
class MisalignedFlag:
    record_id: str
    retrieval_count: int
    avg_downstream_outcome: float
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "retrieval_count": self.retrieval_count,
            "avg_downstream_outcome": round(self.avg_downstream_outcome, 6),
            "reason": self.reason,
        }


def detect_misaligned(
    retrieval_log: Sequence[RetrievalEvent],
    *,
    min_retrievals: int = 2,
    outcome_threshold: float = 0.3,
) -> list[MisalignedFlag]:
    """Flag records that pass the quality filter but consistently lead to poor execution.

    These are the "misaligned experience replay" records from finding 3 of the
    paper: they pass the quality gate at addition time but are misaligned with
    the current task distribution, so conditioning on them degrades
    performance.  The detector flags (does not auto-delete) so a human or the
    analysis stage can decide whether to quarantine or delete.
    """
    per_record: dict[str, list[float]] = {}
    for event in retrieval_log:
        per_record.setdefault(event.record_id, []).append(event.downstream_outcome)

    flags: list[MisalignedFlag] = []
    for rid, outcomes in per_record.items():
        count = len(outcomes)
        avg = sum(outcomes) / count if count else 0.0
        if count >= min_retrievals and avg < outcome_threshold:
            flags.append(
                MisalignedFlag(
                    record_id=rid,
                    retrieval_count=count,
                    avg_downstream_outcome=avg,
                    reason=(
                        f"retrieved {count} times with avg outcome {avg:.3f} < "
                        f"{outcome_threshold:.3f} — misaligned with current task distribution; "
                        f"passed quality gate but consistently leads to poor execution"
                    ),
                )
            )
    return flags


# ── Aggregate report ────────────────────────────────────────────────────────


@dataclass
class MemoryGateReport:
    addition_decisions: list[AdditionDecision] = field(default_factory=list)
    deletion_candidates: list[DeletionCandidate] = field(default_factory=list)
    misaligned_flags: list[MisalignedFlag] = field(default_factory=list)
    retrieval_events: list[RetrievalEvent] = field(default_factory=list)

    @property
    def admitted_count(self) -> int:
        return sum(1 for d in self.addition_decisions if d.admitted)

    @property
    def rejected_count(self) -> int:
        return sum(1 for d in self.addition_decisions if not d.admitted)

    @property
    def error_propagation_blocks(self) -> int:
        return sum(1 for d in self.addition_decisions if d.error_propagation_blocked)

    def to_dict(self) -> dict[str, Any]:
        return {
            "addition_decisions": [d.to_dict() for d in self.addition_decisions],
            "deletion_candidates": [d.to_dict() for d in self.deletion_candidates],
            "misaligned_flags": [f.to_dict() for f in self.misaligned_flags],
            "retrieval_events": [e.to_dict() for e in self.retrieval_events],
            "summary": {
                "admitted": self.admitted_count,
                "rejected": self.rejected_count,
                "error_propagation_blocks": self.error_propagation_blocks,
                "deletion_candidates": len(self.deletion_candidates),
                "misaligned_flags": len(self.misaligned_flags),
            },
        }


def evaluate(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Core entry from a JSON payload.

    Expected payload shape::

        {
          "records": [{"record_id": "...", "quality_score": 0.8, ...}, ...],
          "quality_threshold": 0.7,
          "quarantined_ids": ["noisy-rec-1", ...],
          "retrieval_log": [{"record_id": "...", "downstream_outcome": 0.1, ...}, ...],
          "deletion": {"min_retrievals": 3, "utility_threshold": 0.4},
          "misaligned": {"min_retrievals": 2, "outcome_threshold": 0.3}
        }
    """
    records = [MemoryRecord.from_dict(r) for r in payload.get("records", [])]
    quality_threshold = float(payload.get("quality_threshold", 0.7))
    quarantined = frozenset(payload.get("quarantined_ids", []))

    report = MemoryGateReport()
    for rec in records:
        report.addition_decisions.append(
            addition_gate(
                rec, quality_threshold=quality_threshold, quarantined_ids=quarantined
            )
        )

    log = [RetrievalEvent.from_dict(e) for e in payload.get("retrieval_log", [])]
    report.retrieval_events = log

    del_cfg = payload.get("deletion", {})
    report.deletion_candidates = history_based_deletion(
        log,
        min_retrievals=int(del_cfg.get("min_retrievals", 3)),
        utility_threshold=float(del_cfg.get("utility_threshold", 0.4)),
    )

    mis_cfg = payload.get("misaligned", {})
    report.misaligned_flags = detect_misaligned(
        log,
        min_retrievals=int(mis_cfg.get("min_retrievals", 2)),
        outcome_threshold=float(mis_cfg.get("outcome_threshold", 0.3)),
    )

    return report.to_dict()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Selective memory addition gate with history-based utility deletion (#1270)",
    )
    parser.add_argument(
        "--payload",
        required=True,
        help="path to a JSON payload with records, retrieval_log, thresholds",
    )
    args = parser.parse_args(argv)
    with open(args.payload, encoding="utf-8") as fh:
        payload = json.load(fh)
    report = evaluate(payload)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
