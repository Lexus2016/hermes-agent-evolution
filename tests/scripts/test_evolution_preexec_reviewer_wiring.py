"""Wiring tests for evolution_preexec_reviewer.py (#1271).

Verifies the pre-execution reviewer: trivial/high-confidence calls are
skipped (latency gate), non-trivial low-confidence calls are reviewed,
missing required args are caught, the over-skepticism guard does NOT
reject tool-only responses, and the gate metrics compute a
Benefit-to-Risk ratio with net-negative flagging.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import evolution_preexec_reviewer as pr  # noqa: E402


def _sample_payload() -> dict:
    return {
        "calls": [
            # Non-trivial tool, low confidence, tool-only response -> reviewed + approved.
            {
                "tool": "patch",
                "sub_goal": "fix typo",
                "args": {"path": "f.py", "old_string": "a", "new_string": "b"},
                "confidence": 0.5,
                "is_tool_only_response": True,
            },
            # Non-trivial tool, low confidence, MISSING required args -> rejected.
            {
                "tool": "patch",
                "sub_goal": "fix typo",
                "args": {"path": "f.py"},
                "confidence": 0.5,
                "is_tool_only_response": True,
            },
            # Trivial tool (read_file) -> not reviewed (latency gate).
            {
                "tool": "read_file",
                "sub_goal": "read",
                "args": {"path": "f.py"},
                "confidence": 0.5,
                "is_tool_only_response": True,
            },
            # High-confidence non-trivial call -> not reviewed.
            {
                "tool": "patch",
                "sub_goal": "fix",
                "args": {"path": "f.py", "old_string": "a", "new_string": "b"},
                "confidence": 0.95,
                "is_tool_only_response": True,
            },
        ],
        "confidence_threshold": 0.8,
        "required_args": {"patch": ["path", "old_string", "new_string"]},
        "gates": [
            # Net-positive gate: help > harm.
            {
                "gate_name": "merge_verification",
                "helpful_blocks": 5,
                "harmful_blocks": 1,
                "base_agent_errors": 10,
                "correct_responses": 40,
            },
            # Net-negative gate: harm > help (ratio < 1.0).
            {
                "gate_name": "bad_gate",
                "helpful_blocks": 1,
                "harmful_blocks": 5,
                "base_agent_errors": 10,
                "correct_responses": 40,
            },
        ],
    }


def test_trivial_tool_not_reviewed():
    report = pr.evaluate(_sample_payload())
    v = report["verdicts"][2]  # read_file call
    assert v["reviewed"] is False
    assert v["approve"] is True


def test_high_confidence_not_reviewed():
    report = pr.evaluate(_sample_payload())
    v = report["verdicts"][3]
    assert v["reviewed"] is False


def test_missing_required_args_rejected():
    report = pr.evaluate(_sample_payload())
    v = report["verdicts"][1]  # patch with only path
    assert v["reviewed"] is True
    assert v["approve"] is False
    assert "missing" in v["feedback"].lower()
    assert v["reason"] == "missing_required_args"


def test_tool_only_response_approved_overskepticism_guard():
    report = pr.evaluate(_sample_payload())
    v = report["verdicts"][0]  # patch, complete args, tool-only
    assert v["reviewed"] is True
    assert v["approve"] is True
    assert "over-skepticism" in v["reason"] or "complete" in v["reason"]


def test_reviewer_prompt_has_overskepticism_guardline():
    report = pr.evaluate(_sample_payload())
    prompt = report["reviewer_prompt"]
    assert "[CRITICAL]" in prompt
    assert "Tool-only responses are complete" in prompt


def test_gate_metrics_benefit_to_risk_computed():
    report = pr.evaluate(_sample_payload())
    gm = report["gate_metrics"]
    per_gate = {g["gate_name"]: g for g in gm["per_gate"]}
    assert "merge_verification" in per_gate
    assert per_gate["merge_verification"]["benefit_to_risk"] is not None
    assert per_gate["merge_verification"]["net_negative"] is False


def test_net_negative_gate_flagged():
    report = pr.evaluate(_sample_payload())
    gm = report["gate_metrics"]
    assert "bad_gate" in gm["net_negative_gates"]
    per_gate = {g["gate_name"]: g for g in gm["per_gate"]}
    assert per_gate["bad_gate"]["net_negative"] is True
    assert per_gate["bad_gate"]["benefit_to_risk"] < 1.0


def test_at_least_one_ratio_reported():
    report = pr.evaluate(_sample_payload())
    gm = report["gate_metrics"]
    assert len(gm["ratios_reported"]) >= 1


def test_main_returns_zero(tmp_path, capsys):
    payload_file = tmp_path / "payload.json"
    payload_file.write_text(json.dumps(_sample_payload()), encoding="utf-8")
    rc = pr.main(["--payload", str(payload_file)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "verdicts" in out
