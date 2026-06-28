"""Tests for scripts/evolution_evaluator.py — deterministic evaluator-optimizer
gate for the orchestrator-workers research loop (#230)."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from evolution_evaluator import (  # noqa: E402
    ACCEPT,
    DEFAULT_RUBRIC,
    EXIT_ACCEPT,
    EXIT_BAD_INPUT,
    EXIT_OPTIMIZE,
    EXIT_STOP_BUDGET,
    OPTIMIZE,
    STOP_BUDGET,
    decide,
    evaluate_candidates,
    main,
    score_candidate,
)


class TestScoreCandidate:
    def test_weighted_mean_over_full_rubric(self):
        # All criteria present, all 1.0 -> 1.0 regardless of weights.
        s = score_candidate(
            {"relevance": 1, "evidence": 1, "specificity": 1, "correctness": 1},
            DEFAULT_RUBRIC,
        )
        assert s == 1.0

    def test_partial_rubric_renormalizes(self):
        # Only "relevance" scored -> result is just that criterion's value,
        # NOT deflated by the missing ones (renormalized over present weights).
        assert score_candidate({"relevance": 0.5}, DEFAULT_RUBRIC) == 0.5

    def test_weights_actually_weight(self):
        # correctness weight (1.2) dominates relevance (1.0): a high-correctness
        # / low-relevance candidate scores above the plain average.
        s = score_candidate({"relevance": 0.0, "correctness": 1.0}, DEFAULT_RUBRIC)
        assert abs(s - (1.2 * 1.0) / (1.0 + 1.2)) < 1e-9

    def test_out_of_range_and_nonnumeric_clamped(self):
        assert score_candidate({"relevance": 5}, {"relevance": 1.0}) == 1.0
        assert score_candidate({"relevance": -3}, {"relevance": 1.0}) == 0.0
        assert score_candidate({"relevance": "nope"}, {"relevance": 1.0}) == 0.0

    def test_no_rubric_overlap_is_zero(self):
        # Candidate scores criteria not in the rubric -> cannot be judged -> 0.
        assert score_candidate({"unknown_metric": 1.0}, DEFAULT_RUBRIC) == 0.0


class TestEvaluateCandidates:
    def test_sorted_best_first(self):
        cands = [
            {"scores": {"relevance": 0.2}},
            {"scores": {"relevance": 0.9}},
            {"scores": {"relevance": 0.5}},
        ]
        ranking = evaluate_candidates(cands)
        assert [i for i, _ in ranking] == [1, 2, 0]

    def test_tie_prefers_earlier_index(self):
        cands = [
            {"scores": {"relevance": 0.5}},
            {"scores": {"relevance": 0.5}},
        ]
        ranking = evaluate_candidates(cands)
        assert ranking[0][0] == 0  # earlier worker pass wins a tie

    def test_malformed_candidate_scores_to_zero(self):
        cands = [{"scores": "not-a-dict"}, {"no_scores_key": True}, "totally-bad"]
        ranking = evaluate_candidates(cands)  # must not crash
        assert all(score == 0.0 for _, score in ranking)


class TestDecideVerdicts:
    def _cand(self, val):
        return {"scores": {"relevance": val, "evidence": val, "specificity": val, "correctness": val}}

    def test_accept_when_best_meets_threshold(self):
        r = decide([self._cand(0.6), self._cand(0.8)], threshold=0.75, current_pass=1, max_passes=3)
        assert r["verdict"] == ACCEPT
        assert r["best_index"] == 1
        assert r["best_score"] == 0.8

    def test_optimize_when_below_and_budget_remains(self):
        r = decide([self._cand(0.5)], threshold=0.75, current_pass=1, max_passes=3)
        assert r["verdict"] == OPTIMIZE
        assert r["passes_remaining"] == 2
        assert r["best_index"] == 0

    def test_stop_budget_when_below_and_exhausted(self):
        # Last allowed pass, still below bar -> terminate with best-so-far.
        r = decide([self._cand(0.5)], threshold=0.75, current_pass=3, max_passes=3)
        assert r["verdict"] == STOP_BUDGET
        assert r["passes_remaining"] == 0
        assert r["best_index"] == 0  # best-so-far still surfaced

    def test_loop_is_bounded_and_terminates(self):
        # Success criterion #3: the loop terminates within a bounded number of
        # passes. Even with candidates that NEVER meet the bar, walking the pass
        # counter up to max_passes must end in a terminal (non-OPTIMIZE) verdict.
        max_passes = 4
        verdicts = [
            decide([self._cand(0.1)], threshold=0.9, current_pass=p, max_passes=max_passes)["verdict"]
            for p in range(1, max_passes + 1)
        ]
        assert verdicts[:-1] == [OPTIMIZE] * (max_passes - 1)
        assert verdicts[-1] == STOP_BUDGET  # guaranteed terminal at the budget

    def test_no_candidates_with_budget_optimizes(self):
        r = decide([], threshold=0.75, current_pass=1, max_passes=2)
        assert r["verdict"] == OPTIMIZE
        assert r["best_index"] is None and r["best_score"] == 0.0

    def test_no_candidates_no_budget_stops(self):
        r = decide([], threshold=0.75, current_pass=2, max_passes=2)
        assert r["verdict"] == STOP_BUDGET
        assert r["best_index"] is None

    def test_max_passes_one_never_optimizes(self):
        # A single-shot budget (max_passes=1) can only ACCEPT or STOP_BUDGET.
        below = decide([self._cand(0.1)], threshold=0.75, current_pass=1, max_passes=1)
        assert below["verdict"] == STOP_BUDGET
        above = decide([self._cand(0.9)], threshold=0.75, current_pass=1, max_passes=1)
        assert above["verdict"] == ACCEPT

    def test_degenerate_pass_and_budget_coerced(self):
        # pass 0 / max_passes 0 must be coerced to >=1, not crash or loop forever.
        r = decide([self._cand(0.1)], threshold=0.75, current_pass=0, max_passes=0)
        assert r["pass"] == 1 and r["max_passes"] == 1
        assert r["verdict"] == STOP_BUDGET

    def test_ranking_present_in_record(self):
        r = decide([self._cand(0.2), self._cand(0.9)], threshold=0.75)
        assert r["ranking"][0][0] == 1  # best candidate index first


class TestCli:
    def _run(self, argv, monkeypatch, capsys, stdin_text=None):
        if stdin_text is not None:
            import io

            monkeypatch.setattr(sys, "stdin", io.StringIO(stdin_text))
        rc = main(argv)
        out = capsys.readouterr()
        return rc, out

    def test_accept_exit_code_and_json(self, tmp_path, capsys):
        p = tmp_path / "cands.json"
        p.write_text(json.dumps([{"scores": {"relevance": 0.9, "evidence": 0.9,
                                              "specificity": 0.9, "correctness": 0.9}}]),
                     encoding="utf-8")
        rc = main(["evolution_evaluator.py", "--threshold", "0.75", str(p)])
        payload = json.loads(capsys.readouterr().out)
        assert rc == EXIT_ACCEPT
        assert payload["verdict"] == ACCEPT

    def test_optimize_exit_code(self, tmp_path):
        p = tmp_path / "cands.json"
        p.write_text(json.dumps([{"scores": {"relevance": 0.4}}]), encoding="utf-8")
        rc = main(["evolution_evaluator.py", "--threshold", "0.75",
                   "--pass", "1", "--max-passes", "3", str(p)])
        assert rc == EXIT_OPTIMIZE

    def test_stop_budget_exit_code(self, tmp_path):
        p = tmp_path / "cands.json"
        p.write_text(json.dumps([{"scores": {"relevance": 0.4}}]), encoding="utf-8")
        rc = main(["evolution_evaluator.py", "--threshold", "0.75",
                   "--pass", "3", "--max-passes", "3", str(p)])
        assert rc == EXIT_STOP_BUDGET

    def test_stdin_and_candidates_wrapper(self, monkeypatch, capsys):
        rc, out = self._run(
            ["evolution_evaluator.py", "--threshold", "0.5"],
            monkeypatch, capsys,
            stdin_text=json.dumps({"candidates": [{"scores": {"relevance": 0.9}}]}),
        )
        assert rc == EXIT_ACCEPT
        assert json.loads(out.out)["verdict"] == ACCEPT

    def test_bad_json_exit_2(self, tmp_path, capsys):
        p = tmp_path / "bad.json"
        p.write_text("{ not json", encoding="utf-8")
        rc = main(["evolution_evaluator.py", str(p)])
        assert rc == EXIT_BAD_INPUT
        assert "not valid JSON" in capsys.readouterr().err

    def test_non_list_payload_exit_2(self, tmp_path, capsys):
        p = tmp_path / "obj.json"
        p.write_text(json.dumps({"unexpected": "shape"}), encoding="utf-8")
        rc = main(["evolution_evaluator.py", str(p)])
        assert rc == EXIT_BAD_INPUT

    def test_unknown_flag_exit_2(self, capsys):
        rc = main(["evolution_evaluator.py", "--bogus", "1"])
        assert rc == EXIT_BAD_INPUT

    def test_bad_numeric_flag_exit_2(self, capsys):
        rc = main(["evolution_evaluator.py", "--threshold", "high"])
        assert rc == EXIT_BAD_INPUT
