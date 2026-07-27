# -*- coding: utf-8 -*-
"""Tests for :mod:`evolution.lib.stage_result` (issue #1338)."""

from __future__ import annotations

from evolution.lib.stage_result import StageResult


class TestStageResultSerialisation:
    """to_dict / from_dict round-trip."""

    def test_roundtrip_basic(self):
        sr = StageResult(
            result={"selected": [1, 2, 3]},
            evidence_pointers=["/path/a.json", "/path/b.json"],
            confidence=72,
            stage="local_triage",
            timestamp="2026-07-27T10:00:00Z",
        )
        d = sr.to_dict()
        sr2 = StageResult.from_dict(d)
        assert sr2.result == sr.result
        assert sr2.evidence_pointers == sr.evidence_pointers
        assert sr2.confidence == sr.confidence
        assert sr2.stage == sr.stage
        assert sr2.timestamp == sr.timestamp

    def test_roundtrip_empty_defaults(self):
        sr = StageResult()
        d = sr.to_dict()
        sr2 = StageResult.from_dict(d)
        assert sr2.result is None
        assert sr2.evidence_pointers == []
        assert sr2.confidence == 0
        assert sr2.stage == ""
        assert sr2.timestamp == ""

    def test_to_dict_returns_plain_types(self):
        """to_dict output must be JSON-serialisable."""
        import json

        sr = StageResult(
            result={"k": "v"},
            evidence_pointers=["a", "b"],
            confidence=50,
            stage="test",
            timestamp="now",
        )
        json.dumps(sr.to_dict())  # must not raise


class TestStageResultWrap:
    """The ``wrap`` convenience factory."""

    def test_wrap_default_confidence_with_evidence(self):
        """When evidence exists but confidence unset, default to 50."""
        sr = StageResult.wrap(
            {"data": True},
            evidence_pointers=["x"],
        )
        assert sr.confidence == 50

    def test_wrap_explicit_confidence_preserved(self):
        sr = StageResult.wrap({}, evidence_pointers=["x"], confidence=80)
        assert sr.confidence == 80

    def test_wrap_no_evidence_confidence_stays_zero(self):
        sr = StageResult.wrap({})
        assert sr.confidence == 0
        assert sr.evidence_pointers == []

    def test_wrap_clamps_confidence(self):
        assert StageResult.wrap({}, confidence=150).confidence == 100
        assert StageResult.wrap({}, confidence=-10).confidence == 0

    def test_wrap_accepts_none_pointers(self):
        sr = StageResult.wrap({}, evidence_pointers=None)
        assert sr.evidence_pointers == []

    def test_wrap_sets_stage_and_timestamp(self):
        sr = StageResult.wrap(
            {},
            stage="analysis",
            timestamp="2026-01-01T00:00:00Z",
        )
        assert sr.stage == "analysis"
        assert sr.timestamp == "2026-01-01T00:00:00Z"


class TestStageResultImmutabilityOfPointers:
    """Evidence_pointers list must not be shared across instances."""

    def test_default_factory_isolation(self):
        sr1 = StageResult()
        sr2 = StageResult()
        sr1.evidence_pointers.append("shared?")
        assert sr2.evidence_pointers == []

    def test_to_dict_does_not_leak_internal_list(self):
        sr = StageResult(evidence_pointers=["a"])
        d = sr.to_dict()
        d["evidence_pointers"].append("b")
        assert sr.evidence_pointers == ["a"]
