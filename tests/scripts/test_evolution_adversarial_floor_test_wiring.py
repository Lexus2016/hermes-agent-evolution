"""Wiring tests for evolution_adversarial_floor_test.py (#1267).

Verifies the BenchJack null-agent floor test evaluates correctly over a
sample payload: every metric gets a floor-test result per strategy, a
gameable metric is flagged, isolation is detected, and judge-prompt
injection is caught. Covers the success-criteria invariants, NOT a
snapshot of current data.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import evolution_adversarial_floor_test as ft  # noqa: E402


def _sample_payload() -> dict:
    """A payload with one clean metric, one gameable metric, an isolation
    violation, and a judge prompt with a raw agent-content interpolation."""
    return {
        "metrics": [
            # Clean metric: floor 0, binary, no judge, isolated evaluator.
            {
                "name": "merge_success",
                "floor": 0.0,
                "ceiling": 1.0,
                "kind": "binary",
                "uses_llm_judge": False,
                "judge_delimits_agent_content": True,
                "evaluator_shares_agent_writes": False,
            },
            # Gameable: a submission-count metric an empty patch passes.
            {
                "name": "patches_submitted",
                "floor": 0.0,
                "ceiling": 1.0,
                "kind": "submission_count",
                "uses_llm_judge": False,
                "judge_delimits_agent_content": True,
                "evaluator_shares_agent_writes": False,
            },
            # Gameable: LLM judge with un-delimited agent content (injection vector).
            {
                "name": "judge_quality",
                "floor": 0.0,
                "ceiling": 1.0,
                "kind": "continuous",
                "uses_llm_judge": True,
                "judge_delimits_agent_content": False,
                "evaluator_shares_agent_writes": False,
            },
        ],
        "isolation": {
            "verifier_context": "subagent-impl-1267",
            "implementer_context": "subagent-impl-1267",
        },
        "judge_templates": {
            "merge_judge": "Score the agent response: {agent_response}\n",
        },
        "tolerance": 1e-9,
    }


def test_evaluate_runs_all_strategies_per_metric():
    report = ft.evaluate(_sample_payload())
    # 3 metrics x 5 default strategies = 15 results.
    assert len(report["metric_results"]) == 15
    # Every metric appears in the results.
    metrics_seen = {r["metric"] for r in report["metric_results"]}
    assert metrics_seen == {"merge_success", "patches_submitted", "judge_quality"}


def test_gameable_metric_is_flagged():
    report = ft.evaluate(_sample_payload())
    # patches_submitted is gameable by the empty-patch strategy (scores at
    # ceiling above the floor); judge_quality is gameable by prompt-injection.
    # merge_success (binary, floor 0) is also correctly flagged because the
    # random-agent strategy scores at chance (0.5) which is above the floor —
    # a binary metric whose floor is below chance IS gameable by a random agent.
    failed = set(report["failed_metrics"])
    assert "patches_submitted" in failed
    assert "judge_quality" in failed
    assert "merge_success" in failed  # binary, floor 0 < chance 0.5


def test_isolation_violation_detected():
    report = ft.evaluate(_sample_payload())
    iso = report["isolation"]
    assert iso is not None
    assert iso["isolated"] is False
    assert "SWE-bench" in iso["reason"] or "shares" in iso["reason"]


def test_judge_prompt_injection_caught():
    report = ft.evaluate(_sample_payload())
    assert len(report["judge_findings"]) >= 1
    keys = {f["pattern_key"] for f in report["judge_findings"]}
    assert "RAW_INTERPOLATION" in keys


def test_all_passed_is_false_when_any_fail():
    report = ft.evaluate(_sample_payload())
    assert report["all_passed"] is False


def test_clean_payload_all_passed():
    """A payload with a metric whose floor equals the chance baseline (so the
    random-agent strategy scores AT the floor, not above it), an isolated
    verifier, and clean judge templates passes the gate."""
    clean = {
        # A binary metric with floor = 0.5 (chance level): the random-agent
        # strategy scores ceiling*0.5 = 0.5 which equals the floor, so it
        # passes. The empty-patch and injection strategies stay at the floor
        # for a binary non-submission-count metric.
        "metrics": [
            {
                "name": "chance_metric",
                "floor": 0.5,
                "ceiling": 1.0,
                "kind": "binary",
                "uses_llm_judge": False,
                "judge_delimits_agent_content": True,
                "evaluator_shares_agent_writes": False,
            },
        ],
        "isolation": {
            "verifier_context": "verifier-ctx",
            "implementer_context": "impl-ctx",
        },
        "judge_templates": {"clean_judge": "Score the result: <result>data</result>"},
    }
    report = ft.evaluate(clean)
    assert report["all_passed"] is True, f"failed: {report['failed_metrics']}"
    assert report["failed_metrics"] == []


def test_main_returns_nonzero_when_gate_fails(tmp_path):
    payload_file = tmp_path / "payload.json"
    payload_file.write_text(json.dumps(_sample_payload()), encoding="utf-8")
    rc = ft.main(["--payload", str(payload_file)])
    # Gate fails -> return 1.
    assert rc == 1
