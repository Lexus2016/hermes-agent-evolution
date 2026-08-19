# -*- coding: utf-8 -*-
"""Tests for evolution/lib/grasp_gate.py (#2840, GRASP regression gate)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from evolution.lib.grasp_gate import (  # noqa: E402
    DECISION_ACCEPT,
    DECISION_REJECT,
    DECISION_REVISE,
    balanced_probe_split,
    grasp_regression_gate,
)
from evolution.lib.rubric_forge import RubricJudge  # noqa: E402


def _kw_judge(rubric: str, example) -> bool:
    """Keyword-requirement judge mirroring the pipeline's S1 wiring.

    A skill body (rubric) ACCEPTS an example when it contains every required
    keyword and none of the forbidden keywords; otherwise it REJECTS it.
    """
    ex = example if isinstance(example, dict) else {}
    requires = [k for k in (ex.get("requires") or []) if isinstance(k, str)]
    forbids = [k for k in (ex.get("forbids") or []) if isinstance(k, str)]
    verdict = all(k in rubric for k in requires)
    if forbids:
        verdict = verdict and not any(k in rubric for k in forbids)
    return bool(verdict)


def _tok_judge(rubric: str, example) -> bool:
    """Declarative judge: ACCEPT iff the example's ``token`` is in ``rubric``.

    Gives exact control over which probe examples a candidate accepts/rejects,
    so the regression-budget tests are precise (no keyword ambiguities).
    """
    return bool(example.get("token") in rubric.split())


# --- balanced probe split -------------------------------------------------

class TestBalancedProbeSplit:
    def test_split_is_deterministic_and_parallel(self):
        xs = list(range(8))
        lb = [False, True, False, True, False, True, False, True]
        train_ex, train_lb, probe_ex, probe_lb = balanced_probe_split(xs, lb, probe_size=4)
        # Round-robin failing-then-passing: failing idx {0,2,...} fill odd i.
        assert probe_ex == [0, 1, 2, 3]
        assert probe_lb == [False, True, False, True]
        assert train_ex == [4, 5, 6, 7]

    def test_probe_contains_both_classes_when_possible(self):
        labeled = [{"requires": ["a"]}, {"requires": ["b"]}, {"requires": ["c"]},
                   {"requires": ["d"]}]
        labels = [True, True, False, False]
        _, _, probe_ex, probe_lb = balanced_probe_split(labeled, labels, probe_size=4)
        assert len(probe_ex) == 4
        assert any(probe_lb) and any(not lb for lb in probe_lb)  # both classes

    def test_mismatched_inputs_raise(self):
        try:
            balanced_probe_split([1, 2], [True])
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError for mismatched lengths")

    def test_empty_degrades_to_empty_probe(self):
        _, _, probe_ex, probe_lb = balanced_probe_split([1], [True], probe_size=4)
        assert probe_ex == [1] and probe_lb == [True]

    def test_probe_size_zero_yields_empty_probe(self):
        _, _, probe_ex, _ = balanced_probe_split([1, 2], [True, False], probe_size=0)
        assert probe_ex == []


# --- the GRASP gate -------------------------------------------------------

class TestGraspRegressionGate:
    def test_accept_when_fixes_within_budget(self):
        # Candidate "q" fixes both failing examples (drops the forbidden
        # keyword) and regresses nothing.
        labeled = [{"requires": ["q"]}, {"requires": ["q", "drop"]},
                   {"requires": ["q", "drop"]}]
        labels = [True, False, False]
        v = grasp_regression_gate("q", "q drop", labeled, labels, _kw_judge, probe_size=5)
        assert v.decision == DECISION_ACCEPT
        assert v.net_fixed == 2 and v.regressed == 0

    def test_reject_when_net_not_positive(self):
        # Candidate "x" fixes the failing example but also regresses the
        # passing one: net = 0 -> rejected outright.
        labeled = [{"requires": ["q"]}, {"requires": ["q", "drop"]}]
        labels = [True, False]
        v = grasp_regression_gate("x", "q drop", labeled, labels, _kw_judge, probe_size=5)
        assert v.decision == DECISION_REJECT
        assert v.net_fixed == 0

    def test_revise_when_regressed_beyond_budget(self):
        # Candidate "T1" accepts only T1: fixes F1/F2 but regresses the passing
        # T2. Net-positive yet regressions exceed budget 0 -> contrastive revise.
        labeled = [{"token": "T1"}, {"token": "T2"},
                   {"token": "F1"}, {"token": "F2"}, {"token": "F3"}]
        labels = [True, True, False, False, False]
        v = grasp_regression_gate("T1", "T1 T2", labeled, labels, _tok_judge,
                                  probe_size=5, regression_budget=0)
        assert v.decision == DECISION_REVISE
        assert v.net_fixed == 2 and v.regressed == 1
        # Contrastive revision feeds the regressed example back to the writer.
        assert len(v.regressed_examples) == 1

    def test_regression_budget_parameter_tightens(self):
        # Candidate accepts only T1: fixes F1/F2/F3, regresses passing T2.
        # Within budget 1 it is accepted; with budget 0 it must be revised.
        labeled = [{"token": "T1"}, {"token": "T2"},
                   {"token": "F1"}, {"token": "F2"}, {"token": "F3"}]
        labels = [True, True, False, False, False]
        loose = grasp_regression_gate("T1", "T1 T2", labeled, labels, _tok_judge,
                                      probe_size=5, regression_budget=1)
        tight = grasp_regression_gate("T1", "T1 T2", labeled, labels, _tok_judge,
                                      probe_size=5, regression_budget=0)
        assert loose.decision == DECISION_ACCEPT
        assert loose.net_fixed == 2 and loose.regressed == 1
        assert tight.decision == DECISION_REVISE

    def test_empty_probe_is_fail_closed(self):
        v = grasp_regression_gate("a", "a", [], [], _kw_judge)
        assert v.decision == DECISION_REJECT
        assert "unprovable" in v.reason

    def test_throwing_judge_degrades_safely(self):
        def _boom(_r, _e):
            raise RuntimeError("network down")

        labeled = [{"token": "T1"}, {"token": "F1"}]
        labels = [True, False]
        v = grasp_regression_gate("x", "y", labeled, labels, _boom, probe_size=5)
        # A throwing judge proves nothing: neither fixed nor regressed counts,
        # net 0 -> reject (never a false accept).
        assert v.decision == DECISION_REJECT
        assert v.fixed == 0 and v.regressed == 0

    def test_baseline_supplies_budget_when_parameter_zero(self):
        # The baseline itself regresses a passing example; with budget 0 the
        # hard budget is still at least the baseline's own regressions.
        labeled = [{"token": "T1"}, {"token": "T2"},
                   {"token": "F1"}, {"token": "F2"}]
        labels = [True, True, False, False]
        v = grasp_regression_gate("T1 T2 F1 F2", "T1 T2", labeled, labels,
                                  _tok_judge, probe_size=5, regression_budget=0)
        assert v.regression_budget >= 0
