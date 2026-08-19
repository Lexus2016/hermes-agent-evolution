# -*- coding: utf-8 -*-
"""Tests for :mod:`evolution.lib.temporal_validity` (issue #2842)."""

from __future__ import annotations

from evolution.lib.temporal_validity import (
    EvidenceItem,
    TemporalValidityReport,
    evaluate_temporal_validity,
    parse_iso_epoch,
)

_REF = "2026-08-19T00:00:00Z"  # reference as-of date


class TestParseIsoEpoch:
    def test_parses_utc_z(self):
        # Compute the expected epoch programmatically rather than hardcoding a
        # constant that is easy to mistype.
        import calendar

        expected = calendar.timegm((2026, 8, 19, 0, 0, 0, 0, 0, 0))
        assert parse_iso_epoch("2026-08-19T00:00:00Z") == expected

    def test_rejects_malformed(self):
        assert parse_iso_epoch("not-a-date") is None
        assert parse_iso_epoch(None) is None
        assert parse_iso_epoch("") is None

    def test_rejects_fractional_offsets(self):
        assert parse_iso_epoch("2026-08-19T00:00:00+01:00") is not None


class TestEvaluateTemporalValidity:
    def test_valid_item_passes(self):
        items = [
            EvidenceItem(
                id="a",
                valid_from="2026-01-01T00:00:00Z",
                valid_until="2026-12-31T00:00:00Z",
            )
        ]
        r = evaluate_temporal_validity(items, _REF)
        assert r.total == 1
        assert r.passed == 1
        assert r.pass_rate == 1.0
        assert r.violations == []

    def test_future_dated_from_violates(self):
        items = [EvidenceItem(id="a", valid_from="2026-09-01T00:00:00Z")]
        r = evaluate_temporal_validity(items, _REF)
        assert r.passed == 0
        assert r.violations[0]["reason"] == "future-dated"

    def test_expired_until_violates(self):
        items = [EvidenceItem(id="a", valid_until="2026-01-01T00:00:00Z")]
        r = evaluate_temporal_validity(items, _REF)
        assert r.violations[0]["reason"] == "expired"

    def test_undated_item_skipped_not_violation(self):
        items = [EvidenceItem(id="a")]  # no date at all
        r = evaluate_temporal_validity(items, _REF)
        assert r.passed == 1
        assert r.violations == []

    def test_point_timestamp_future_violates(self):
        items = [EvidenceItem(id="a", timestamp="2026-09-01T00:00:00Z")]
        r = evaluate_temporal_validity(items, _REF)
        assert r.violations[0]["reason"] == "future-dated"

    def test_point_timestamp_past_passes(self):
        items = [EvidenceItem(id="a", timestamp="2026-01-01T00:00:00Z")]
        r = evaluate_temporal_validity(items, _REF)
        assert r.passed == 1

    def test_mixed_set_pass_rate(self):
        items = [
            EvidenceItem(id="ok", valid_until="2026-12-31T00:00:00Z"),
            EvidenceItem(id="bad1", valid_from="2026-09-01T00:00:00Z"),
            EvidenceItem(id="bad2", valid_until="2026-01-01T00:00:00Z"),
            EvidenceItem(id="nodate"),
        ]
        r = evaluate_temporal_validity(items, _REF)
        assert r.total == 4
        assert r.passed == 2
        assert r.pass_rate == 0.5
        assert {v["id"] for v in r.violations} == {"bad1", "bad2"}

    def test_malformed_as_of_fails_open(self):
        r = evaluate_temporal_validity(
            [EvidenceItem(id="a", valid_until="2026-01-01T00:00:00Z")],
            "garbage",
        )
        assert r.total == 0
        assert r.pass_rate == 1.0


class TestReportSerialisation:
    def test_to_dict_from_dict_roundtrip(self):
        items = [EvidenceItem(id="a", valid_from="2026-09-01T00:00:00Z")]
        r = evaluate_temporal_validity(items, _REF)
        d = r.to_dict()
        r2 = TemporalValidityReport.from_dict(d)
        assert r2.as_of == _REF
        assert r2.passed == 0
        assert r2.total == 1

    def test_evidence_item_roundtrip(self):
        e = EvidenceItem(id="x", valid_from="2026-01-01T00:00:00Z", evidence=["p"])
        e2 = EvidenceItem.from_dict(e.to_dict())
        assert e2.id == "x"
        assert e2.valid_from == "2026-01-01T00:00:00Z"
        assert e2.evidence == ["p"]
