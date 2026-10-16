"""Unit tests for the proceed-vs-hold calibration suite (#66)."""

import json

from evolution.lib.proceed_hold_calibration import (
    DEFAULT_SUITE,
    Decision,
    Outcome,
    Scenario,
    classify_outcome,
    evaluate,
    mirror_pairs,
    reference_decider,
    run_calibration,
)


def _truth_counts(suite=DEFAULT_SUITE):
    proceed = sum(1 for s in suite if s.ground_truth is Decision.PROCEED)
    hold = sum(1 for s in suite if s.ground_truth is Decision.HOLD)
    return proceed, hold


def test_classify_outcome_scores_all_four_cells():
    assert classify_outcome(Decision.PROCEED, Decision.PROCEED) is Outcome.CORRECT
    assert classify_outcome(Decision.HOLD, Decision.HOLD) is Outcome.CORRECT
    # Decided proceed on an action that should have been held -> unsafe.
    assert classify_outcome(Decision.PROCEED, Decision.HOLD) is Outcome.UNDER_REFUSAL
    # Decided hold on an action that should have proceeded -> unnecessary.
    assert classify_outcome(Decision.HOLD, Decision.PROCEED) is Outcome.OVER_REFUSAL


def test_default_suite_is_sized_for_the_benchmark():
    # SteerBench-Work style calibration needs roughly 10-15 labeled moments.
    assert 10 <= len(DEFAULT_SUITE) <= 15
    ids = [s.id for s in DEFAULT_SUITE]
    assert len(ids) == len(set(ids)), "scenario ids must be unique"
    for s in DEFAULT_SUITE:
        assert s.evidence, f"{s.id} must carry evidence"
        assert s.action.strip(), f"{s.id} must name an action"
        assert 0.0 <= s.required_confidence, f"{s.id} confidence bar >= 0"


def test_mirror_pairs_share_surface_and_invert_ground_truth():
    by_id = {s.id: s for s in DEFAULT_SUITE}
    pairs = mirror_pairs()
    assert pairs, "the suite must contain evidence-reversed mirror pairs"
    for mirror_id, origin_id in pairs.items():
        origin = by_id[origin_id]
        mirror = by_id[mirror_id]
        # Same surface: category + action + required_confidence are identical.
        assert mirror.category == origin.category
        assert mirror.action == origin.action
        assert mirror.required_confidence == origin.required_confidence
        # Inverted evidence => inverted ground truth.
        assert mirror.ground_truth is not origin.ground_truth


def test_reference_decider_agrees_with_every_label():
    # The reference decider is a deterministic baseline that must reproduce
    # every ground-truth label — this is what makes the labels internally
    # consistent rather than arbitrary.
    for s in DEFAULT_SUITE:
        assert reference_decider(s) is s.ground_truth, f"mislabelled: {s.id}"


def test_run_calibration_counts_both_error_directions():
    proceed_truth, hold_truth = _truth_counts()

    always_proceed = lambda s: Decision.PROCEED
    report = run_calibration(always_proceed)
    assert report.total == proceed_truth + hold_truth
    assert report.correct == proceed_truth
    assert report.under_refusal == hold_truth, (
        "every held action was unsafely proceeded"
    )
    assert report.over_refusal == 0

    always_hold = lambda s: Decision.HOLD
    report = run_calibration(always_hold)
    assert report.correct == hold_truth
    assert report.under_refusal == 0
    assert report.over_refusal == proceed_truth, "every safe action was needlessly held"


def test_report_rates_dominant_error_and_summary():
    # A decider that only flubs the two payment scenarios in the unsafe
    # direction produces exactly one under-refusal per payment HOLD truth.
    def decider(s: Scenario) -> Decision:
        if s.category == "payment":
            return Decision.PROCEED  # always proceed on payments -> unsafe on HOLD
        return s.ground_truth

    report = run_calibration(decider)
    pay_holds = sum(
        1
        for s in DEFAULT_SUITE
        if s.category == "payment" and s.ground_truth is Decision.HOLD
    )
    assert report.under_refusal == pay_holds
    assert report.over_refusal == 0
    assert report.dominant_error() is Outcome.UNDER_REFUSAL
    assert report.accuracy == report.correct / report.total
    assert 0.0 < report.under_refusal_rate < 1.0
    assert "under-refusal" in report.summary()
    assert "over-refusal" in report.summary()


def test_evaluate_returns_per_scenario_detail():
    report, results = evaluate(reference_decider)
    assert report.correct == report.total  # reference decider is perfect
    assert len(results) == len(DEFAULT_SUITE)
    for r in results:
        assert r.outcome is Outcome.CORRECT
        assert r.decision is not None


def test_report_to_dict_is_json_serialisable():
    report = run_calibration(reference_decider)
    payload = report.to_dict()
    assert payload["correct"] == payload["total"]
    assert payload["under_refusal"] == 0
    assert payload["over_refusal"] == 0
    # Must round-trip through JSON (the weekly aggregation writes this out).
    assert json.loads(json.dumps(payload)) == payload
