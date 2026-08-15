#!/usr/bin/env python3
"""GEPA held-out validation + promotion ledger (issue #2232, Slices C+D).

Slice A (``tools/gepa_reflector.py``, #2231) turns evaluation trajectories
into textual critiques. Slice B (``tools/gepa_evolution.py``, #2328) mutates
candidates from those critiques and accumulates them in an ``EvolutionTree``.
This module is the missing last mile: **held-out validation** — evaluate a
candidate against a *disjoint* set of results before trusting it — and
**promotion ledger** — persist only candidates that pass the held-out gate,
with an append-only JSONL ledger as the durable record.

Slices C+D are fused here on purpose (mirroring the reviewer's demand on the
now-closed #2330): a validation gate with no consumer is speculative. The
consumer is the promotion ledger, which is read back by
``scripts/evolution_gepa_optimize.py`` (the live runtime caller) so a
promoted candidate actually becomes the next seed.

Everything is deterministic and pure-Python (no LLM required) so the gate is
unit-testable and safe to run in cron.

Design invariants
-----------------
* **Held-out means held-out**: a candidate may only be validated against
  results that were NOT used to produce it. The gate records the task ids it
  saw and rejects validation against a task it has already consumed.
* **Promotion is idempotent**: promoting the same candidate text twice does
  not double-append; the ledger de-duplicates by content hash.
* **Gate never raises**: dropped/missing results, empty task sets, or a
  failed held-out score all yield a ``"rejected"`` verdict with a reason —
  the pipeline never hard-fails on a promotion.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

from tools.gepa_reflector import VariantResult


def content_hash(text: str) -> str:
    """Stable 12-hex content hash used to identify + de-duplicate candidates."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


@dataclass
class HeldoutDecision:
    """Result of validating one candidate against the held-out set."""

    candidate_id: str
    passed: bool
    verdict: str  # "promoted" | "rejected"
    reason: str = ""
    heldout_pass_rate: float = 0.0
    heldout_n: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "passed": self.passed,
            "verdict": self.verdict,
            "reason": self.reason,
            "heldout_pass_rate": self.heldout_pass_rate,
            "heldout_n": self.heldout_n,
            "metadata": self.metadata,
        }


@dataclass
class PromotionGate:
    """Stateful held-out validation gate (Slice C)."""

    # Task ids already consumed by the candidate-producing run. A validation
    # result whose task is in this set is "tainted" — it was used to produce
    # the candidate — and is excluded from the held-out score.
    seen_tasks: set = field(default_factory=set)
    # Minimum held-out pass rate required to promote (default: 0.5 — at least
    # half the held-out tasks must pass; raise to 1.0 for no-regression gating).
    min_pass_rate: float = 0.5

    def validate(
        self,
        candidate_id: str,
        text: str,
        candidate_results: Iterable[VariantResult],
        heldout_results: Iterable[VariantResult],
    ) -> HeldoutDecision:
        """Validate *candidate_id* against *heldout_results*.

        *candidate_results* are the results that produced the candidate and
        are recorded so they can never count as held-out. Returns an
        appropriate ``HeldoutDecision``; never raises.
        """
        # 1. Record candidate-producing tasks as seen.
        cand_tasks: List[str] = []
        for r in candidate_results:
            cand_tasks.append(r.task)
            self.seen_tasks.add(r.task)

        # 2. Held-out = results whose task is NOT seen.
        heldout: List[VariantResult] = [r for r in heldout_results if r.task not in self.seen_tasks]
        # Taint the held-out set forward too: whatever we validate against is
        # now seen, so it can't be re-used for a later candidate.
        for r in heldout:
            self.seen_tasks.add(r.task)

        if not heldout:
            return HeldoutDecision(
                candidate_id=candidate_id,
                passed=False,
                verdict="rejected",
                reason="no held-out results after excluding seen tasks",
                metadata={"seen_tasks": sorted(self.seen_tasks)},
            )

        passed = sum(1 for r in heldout if r.passed)
        rate = passed / len(heldout)
        ok = rate >= self.min_pass_rate
        return HeldoutDecision(
            candidate_id=candidate_id,
            passed=ok,
            verdict="promoted" if ok else "rejected",
            reason="" if ok else f"held-out pass rate {rate:.2f} below {self.min_pass_rate:.2f}",
            heldout_pass_rate=round(rate, 4),
            heldout_n=len(heldout),
            metadata={"candidate_tasks": sorted(cand_tasks), "heldout_tasks": sorted(r.task for r in heldout)},
        )


class PromotionLedger:
    """Append-only JSONL ledger of promoted candidates (Slice D — the consumer)."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._seen_hashes: set = set()
        self._load_existing()

    def _load_existing(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    h = rec.get("content_hash")
                    if h:
                        self._seen_hashes.add(h)
        except OSError:
            return

    def contains(self, text: str) -> bool:
        """True when *text* has already been promoted (idempotency check)."""
        return content_hash(text) in self._seen_hashes

    def promote(
        self,
        decision: HeldoutDecision,
        text: str,
        *,
        source_generation: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """Persist a promoted candidate; no-op (None) when rejected or duplicate.

        Only a ``verdict == "promoted"`` decision is written. Duplicate content
        is silently skipped (idempotent). Returns the promoted record, or
        ``None``.
        """
        if decision.verdict != "promoted":
            return None
        h = content_hash(text)
        if h in self._seen_hashes:
            return None
        record = {
            "content_hash": h,
            "candidate_id": decision.candidate_id,
            "promoted_at": _dt.datetime.now(_dt.timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z"),
            "heldout_pass_rate": decision.heldout_pass_rate,
            "heldout_n": decision.heldout_n,
            "source_generation": source_generation,
            "text": text,
        }
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._seen_hashes.add(h)
        return record


def promote_candidate(
    gate: PromotionGate,
    ledger: PromotionLedger,
    *,
    candidate_id: str,
    text: str,
    candidate_results: Iterable[VariantResult],
    heldout_results: Iterable[VariantResult],
    source_generation: Optional[int] = None,
) -> Tuple[HeldoutDecision, Optional[Dict[str, Any]]]:
    """One-shot validate-then-promote: the single entry point callers use.

    Returns ``(decision, promoted_record)`` where ``promoted_record`` is
    ``None`` unless the candidate passed the held-out gate AND was not already
    in the ledger.
    """
    decision = gate.validate(candidate_id, text, candidate_results, heldout_results)
    record = ledger.promote(
        decision,
        text,
        source_generation=source_generation,
    )
    return decision, record
