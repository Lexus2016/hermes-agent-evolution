"""Tests for tools.gepa_promotion (issue #2232, Slices C+D)."""

import json

import pytest

from tools.gepa_promotion import (
    HeldoutDecision,
    PromotionGate,
    PromotionLedger,
    content_hash,
    promote_candidate,
)
from tools.gepa_reflector import VariantResult


def _heldout_passing() -> list[VariantResult]:
    return [
        VariantResult(variant="cand", task="h1", passed=True),
        VariantResult(variant="cand", task="h2", passed=True),
    ]


def _candidate_results() -> list[VariantResult]:
    # Tasks used to PRODUCE the candidate — must never count as held-out.
    return [
        VariantResult(variant="cand", task="t1", passed=True),
        VariantResult(variant="cand", task="t2", passed=False),
    ]


def test_content_hash_stable_and_distinct():
    assert content_hash("abc") == content_hash("abc")
    assert content_hash("abc") != content_hash("abd")
    assert len(content_hash("abc")) == 12


def test_gate_promotes_clean_heldout():
    gate = PromotionGate()
    d = gate.validate("c1", "text", _candidate_results(), _heldout_passing())
    assert d.verdict == "promoted"
    assert d.passed is True
    assert d.heldout_n == 2
    assert d.heldout_pass_rate == 1.0


def test_gate_rejects_below_threshold():
    gate = PromotionGate(min_pass_rate=1.0)
    heldout = [
        VariantResult(variant="cand", task="h1", passed=True),
        VariantResult(variant="cand", task="h2", passed=False),
    ]
    d = gate.validate("c1", "text", _candidate_results(), heldout)
    assert d.verdict == "rejected"
    assert d.passed is False
    assert "below" in d.reason


def test_gate_excludes_candidate_tasks_from_heldout():
    gate = PromotionGate()
    # A held-out "result" that reuses a candidate-producing task id must be
    # treated as tainted and dropped from the held-out set.
    heldout = [
        VariantResult(variant="cand", task="t1", passed=False),  # tainted
        VariantResult(variant="cand", task="h1", passed=True),
    ]
    d = gate.validate("c1", "text", _candidate_results(), heldout)
    assert d.heldout_n == 1  # only h1 counts
    assert d.verdict == "promoted"


def test_gate_rejects_when_no_heldout_left():
    gate = PromotionGate()
    heldout = [VariantResult(variant="cand", task="t1", passed=True)]
    d = gate.validate("c1", "text", _candidate_results(), heldout)
    assert d.verdict == "rejected"
    assert "no held-out" in d.reason


def test_ledger_promote_and_idempotency(tmp_path):
    path = str(tmp_path / "promotions.jsonl")
    ledger = PromotionLedger(path)
    d = HeldoutDecision(candidate_id="c1", passed=True, verdict="promoted",
                        heldout_pass_rate=1.0, heldout_n=2)
    rec = ledger.promote(d, "some text")
    assert rec is not None
    assert rec["content_hash"] == content_hash("some text")
    assert rec["candidate_id"] == "c1"
    # duplicate promote is a no-op
    assert ledger.promote(d, "some text") is None
    assert ledger.contains("some text")


def test_ledger_skips_rejected(tmp_path):
    path = str(tmp_path / "promotions.jsonl")
    ledger = PromotionLedger(path)
    d = HeldoutDecision(candidate_id="c1", passed=False, verdict="rejected")
    assert ledger.promote(d, "won't make it") is None
    assert not ledger.contains("won't make it")


def test_ledger_reloads_existing(tmp_path):
    path = str(tmp_path / "promotions.jsonl")
    PromotionLedger(path).promote(
        HeldoutDecision(candidate_id="c1", passed=True, verdict="promoted"),
        "persisted text",
    )
    # A fresh ledger over the same file remembers the prior promotion.
    ledger2 = PromotionLedger(path)
    assert ledger2.contains("persisted text")


def test_promote_candidate_end_to_end(tmp_path):
    path = str(tmp_path / "promotions.jsonl")
    gate = PromotionGate()
    ledger = PromotionLedger(path)
    decision, record = promote_candidate(
        gate, ledger,
        candidate_id="c1", text="improved skill",
        candidate_results=_candidate_results(),
        heldout_results=_heldout_passing(),
        source_generation=1,
    )
    assert decision.verdict == "promoted"
    assert record is not None
    # second call: same content already in ledger → promoted verdict, no record
    decision2, record2 = promote_candidate(
        PromotionGate(), ledger,
        candidate_id="c1b", text="improved skill",
        candidate_results=_candidate_results(),
        heldout_results=_heldout_passing(),
    )
    assert decision2.verdict == "promoted"
    assert record2 is None  # idempotent: already promoted


def test_gate_never_raises_on_empty_inputs():
    gate = PromotionGate()
    d = gate.validate("c1", "text", [], [])
    assert d.verdict == "rejected"
    assert "no held-out" in d.reason
