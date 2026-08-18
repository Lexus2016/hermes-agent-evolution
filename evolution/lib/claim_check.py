# -*- coding: utf-8 -*-
"""Code-enforced claim checks: producer-side grounding of numerical claims (#2809).

OmniScientist pattern (arXiv 2608.13558): claim verification is a
deterministic POST-CONDITION on the artifact, not a trust judgement. Every
numerical claim in a research/analysis artifact must trace to an execution
record — a logged metric line, a computed value in the record set, or a
file-hash pointer — or the claim is flagged UNGROUNDED at generation time.

Judge-side claim extraction/triage live in ``evolution_rubric_judge``
(#2482/#2513 family); this module is the producer-side gate that composes
with them: run it on the artifact BEFORE judging, and surface claim-level
flags rather than a trust decision.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence

__all__ = ["ClaimCheckResult", "build_execution_record", "check_claims_grounded"]

# A numerical claim: a number with a unit-ish suffix, %, x, or a comparator.
_NUM_CLAIM_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:%|percent|x\b|×\b|ms\b|s\b|seconds?|minutes?|hours?|"
    r"tokens?|USD|\$|requests?|calls?|files?|issues?|PRs?|commits?|lines?)\b"
    r"|\b\d+(?:\.\d+)?%"
    r"|\b(?:reduced|improved|increased|decreased|by|from|to|up to|down to)\s+"
    r"\d+(?:\.\d+)?",
    re.IGNORECASE,
)


@dataclass
class ClaimCheckResult:
    """Post-condition verdict on one artifact's numerical claims."""

    grounded: List[str] = field(default_factory=list)
    ungrounded: List[str] = field(default_factory=list)
    passed: bool = True

    @property
    def flags(self) -> List[Dict[str, Any]]:
        """Claim-level flags composable with the judge's triage taxonomy."""
        return [
            {"claim": c, "verdict": "ungrounded_number", "gate": "claim_check"}
            for c in self.ungrounded
        ]


def build_execution_record(
    *sources: str,
    metrics: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    """Deterministic execution record from raw artifact/log text.

    ``sources`` are execution outputs (logs, metric dumps, analysis JSON
    rendered as text). The record collects every number that appears in them
    (the VALUES that were actually produced) plus a content hash per source
    — a pointer any claim can trace to.
    """
    values: set = set()
    for text in sources:
        if not text:
            continue
        for m in re.finditer(r"\d+(?:\.\d+)?", text):
            values.add(m.group(0).rstrip("."))
    return {
        "values": sorted(values),
        "values_set": {v for v in values},
        "source_hashes": [
            hashlib.sha256((s or "").encode("utf-8")).hexdigest()[:16] for s in sources
        ],
        "metrics": dict(metrics or {}),
    }


def _sentences(text: str) -> List[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text or "") if s.strip()]


def _sentence_grounds(
    sentence: str, record: Dict[str, Any]
) -> bool:
    """A numerical sentence is grounded when each claimed number appears in
    the execution record's produced values (exact or scaled-percent form)."""
    claimed = _NUM_CLAIM_RE.findall(sentence)
    if not claimed:
        return True  # not a numerical claim — nothing to ground
    produced: set = record.get("values_set") or set()
    if not produced:
        return False
    numbers = re.findall(r"\d+(?:\.\d+)?", sentence)
    for num in numbers:
        n = num.rstrip(".")
        if n in produced:
            continue
        # A percent may be derived: allow when the base exists (e.g. '47%'
        # with 47 or 0.47 produced) — deterministic numeric normalization.
        try:
            f = float(n)
        except ValueError:
            return False
        if f < 1 and f"{f * 100:g}" in produced:
            continue
        if f >= 1 and f"{f / 100:g}" in produced:
            continue
        return False
    return True


def check_claims_grounded(
    artifact_text: str,
    execution_record: Dict[str, Any],
    *,
    max_claims: int = 200,
) -> ClaimCheckResult:
    """Post-condition: every numerical claim traces to the execution record.

    Sentences carrying numbers that appear NOWHERE in the produced values
    are flagged ungrounded (fabricated or hallucinated figures). The gate
    ``passed`` is False when any ungrounded numerical claim exists — the
    artifact is rejected at generation time unless the caller treats flags
    as advisory.
    """
    result = ClaimCheckResult()
    for sentence in _sentences(artifact_text)[:max_claims]:
        if not _NUM_CLAIM_RE.search(sentence):
            continue
        (result.grounded if _sentence_grounds(sentence, execution_record) else
         result.ungrounded).append(sentence)
    result.passed = not result.ungrounded
    return result
