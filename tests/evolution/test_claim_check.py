# -*- coding: utf-8 -*-
"""Claim Check — producer-side grounding gate tests (#2809)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from evolution.lib.claim_check import (  # noqa: E402
    build_execution_record,
    check_claims_grounded,
)

_LOG = """
funnel 2026-08-18: merged=5 blocked=1 latency 412ms cost $0.42
harvest: 47 sessions scanned, 12 entries appended
pytest: 482 passed, 3 failed
"""


def test_execution_record_collects_values_and_hashes():
    rec = build_execution_record(_LOG)
    for v in ("5", "1", "412", "0.42", "47", "12", "482", "3"):
        assert v in rec["values"], v
    assert len(rec["source_hashes"]) == 1 and len(rec["source_hashes"][0]) == 16


def test_grounded_numbers_pass():
    rec = build_execution_record(_LOG)
    artifact = (
        "The funnel merged 5 PRs with median latency 412ms. "
        "The harvest appended 12 entries from 47 sessions."
    )
    result = check_claims_grounded(artifact, rec)
    assert result.passed is True
    assert result.ungrounded == []
    assert len(result.grounded) == 2


def test_hallucinated_figure_is_flagged_and_fails_gate():
    rec = build_execution_record(_LOG)
    artifact = "The funnel merged 5 PRs, improving throughput by 73%."
    result = check_claims_grounded(artifact, rec)
    assert result.passed is False
    assert len(result.ungrounded) == 1
    assert "73" in result.ungrounded[0]
    flags = result.flags
    assert flags[0]["verdict"] == "ungrounded_number"
    assert flags[0]["gate"] == "claim_check"


def test_percent_normalization_traces_fraction_form():
    rec = build_execution_record("accuracy 0.47")
    result = check_claims_grounded("Accuracy reached 47%.", rec)
    assert result.passed is True  # 0.47 produced → 47% grounded


def test_non_numerical_claims_are_not_gate_subjects():
    rec = build_execution_record(_LOG)
    result = check_claims_grounded(
        "The approach works well and the docs were updated.", rec
    )
    assert result.passed is True and result.grounded == []


def test_empty_record_fails_any_numerical_claim():
    rec = build_execution_record("")
    result = check_claims_grounded("Latency was 412ms.", rec)
    assert result.passed is False  # nothing was produced → nothing grounds it
