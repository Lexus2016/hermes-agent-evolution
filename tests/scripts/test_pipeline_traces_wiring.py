"""Wiring tests for pipeline_traces.py (#1269 / #1272 / #1273).

Verifies the AgentFlow evolving-memory pipeline: stage records carry the
verification-status signal, broadcast_reward writes the final-outcome
reward back to every record in the cycle, offline correlation analysis
identifies at least one stage decision correlated with success, and the
adversarial-floor-test gate REFUSES correlation analysis when
floor_test_passed is False. Covers invariants.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import pipeline_traces as pt  # noqa: E402


def _sample_payload(floor_test_passed: bool = True) -> dict:
    """Two cycles: one successful (research sufficient, impl sufficient),
    one failed (research insufficient, impl insufficient). The correlation
    analysis should find that `research_marked_sufficient` lifts success."""
    records = []
    # Cycle 1: success.
    for stage in pt.PIPELINE_STAGES:
        records.append({
            "cycle_id": "2026-07-25",
            "stage": stage,
            "turn_index": 0,
            "sub_goal": f"{stage} for cycle 1",
            "tool_calls": [{"tool": "terminal"}] if stage == "research" else [],
            "result_summary": "done",
            "verification_status": pt.VerificationStatus.SUFFICIENT,
        })
    # Cycle 2: failure — research insufficient, implementation insufficient.
    for stage in pt.PIPELINE_STAGES:
        records.append({
            "cycle_id": "2026-07-26",
            "stage": stage,
            "turn_index": 0,
            "sub_goal": f"{stage} for cycle 2",
            "tool_calls": [],
            "result_summary": "incomplete",
            "verification_status": pt.VerificationStatus.INSUFFICIENT,
        })
    return {
        "records": records,
        "cycle_outcome": {
            "cycle_id": "2026-07-25",
            "succeeded": True,
            "reward": 1.0,
            "floor_test_passed": floor_test_passed,
        },
        "correlation_min_sample": 1,
    }


def test_stage_records_carry_verification_status():
    report = pt.evaluate(_sample_payload())
    for r in report["records"]:
        assert r["verification_status"] in ("sufficient", "insufficient")


def test_broadcast_reward_writes_to_every_cycle_record():
    report = pt.evaluate(_sample_payload())
    cycle1 = [r for r in report["records"] if r["cycle_id"] == "2026-07-25"]
    assert len(cycle1) == len(pt.PIPELINE_STAGES)
    for r in cycle1:
        assert r["final_outcome_reward"] == 1.0
        assert r["cycle_succeeded"] is True
    # Cycle 2 records are NOT updated (different cycle_id).
    cycle2 = [r for r in report["records"] if r["cycle_id"] == "2026-07-26"]
    for r in cycle2:
        assert r["final_outcome_reward"] is None


def test_broadcast_complete_property():
    report = pt.evaluate(_sample_payload())
    assert report["summary"]["broadcast_complete"] is True


def test_correlation_analysis_identifies_predictive_pattern():
    report = pt.evaluate(_sample_payload())
    corr = report["correlation"]
    assert corr["refused"] is False
    findings = corr["findings"]
    assert len(findings) >= 1
    # research_marked_sufficient should be present and have positive lift
    # (present in cycle 1 which succeeded, absent in cycle 2 which failed).
    research_findings = [
        f for f in findings if f["decision_pattern"] == "research_marked_sufficient"
    ]
    assert len(research_findings) == 1
    assert research_findings[0]["lift"] > 0.0
    assert corr["top_correlation"] is not None


def test_correlation_refused_when_floor_test_fails():
    """The #1267 dependency: correlation analysis is refused when the
    adversarial floor test did not pass."""
    report = pt.evaluate(_sample_payload(floor_test_passed=False))
    corr = report["correlation"]
    assert corr["refused"] is True
    assert "floor test" in corr["reason"].lower()
    assert corr["findings"] == []


def test_correlation_note_is_not_imitation():
    """The SFT-collapse caution: the analysis identifies correlations but
    does NOT distil them into a policy."""
    report = pt.evaluate(_sample_payload())
    corr = report["correlation"]
    assert corr["refused"] is False
    if corr["findings"]:
        assert (
            "NOT distilled" in corr["findings"][0]["note"]
            or "not distil" in corr["note"].lower()
        )


def test_pipeline_stages_complete():
    """All five pipeline stages are defined."""
    assert set(pt.PIPELINE_STAGES) == {
        "research",
        "issues",
        "analysis",
        "implementation",
        "metrics",
    }


def test_main_returns_zero(tmp_path, capsys):
    payload_file = tmp_path / "payload.json"
    payload_file.write_text(json.dumps(_sample_payload()), encoding="utf-8")
    rc = pt.main(["--payload", str(payload_file)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "records" in out
