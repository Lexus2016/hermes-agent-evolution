"""Tests for scripts/evolution_optimizer.py — the bounded evaluator-optimizer
LOOP wiring (#301). The loop feeds candidates through evolution_evaluator.decide
and acts on the verdict: ACCEPT → converged, OPTIMIZE → refine best + re-run,
STOP_BUDGET → best-so-far flagged unconverged. Bounded at max_passes (default 3).

The two open-ended steps (evaluate/refine) are injected seams, so every test
here is deterministic with no network and no model."""

import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from evolution_optimizer import (  # noqa: E402
    DEFAULT_MAX_PASSES,
    EXIT_BAD_INPUT,
    EXIT_CONVERGED,
    EXIT_UNCONVERGED,
    main,
    make_default_evaluate,
    run_optimizer_loop,
)
from evolution_evaluator import ACCEPT, OPTIMIZE, STOP_BUDGET  # noqa: E402


def _verdict(name, best_index=0, best_score=0.0):
    """A minimal decide()-shaped verdict record for stub evaluators."""
    return {
        "verdict": name,
        "best_index": best_index,
        "best_score": best_score,
        "threshold": 0.75,
        "pass": 1,
        "max_passes": 3,
        "passes_remaining": 0,
        "ranking": [],
        "reason": name,
    }


def _scripted_evaluate(verdicts):
    """Return an ``evaluate`` seam that yields the given verdicts pass by pass,
    recording which (candidates, pass) it was called with."""
    calls = []

    def _evaluate(candidates, current_pass):
        calls.append((list(candidates), current_pass))
        v = verdicts[min(current_pass - 1, len(verdicts) - 1)]
        return candidates, v

    _evaluate.calls = calls
    return _evaluate


def _recording_refine():
    """A ``refine`` seam that records its (best, pass) args and returns a fresh
    single-candidate list tagged with the pass it was produced on."""
    calls = []

    def _refine(best, current_pass):
        calls.append((best, current_pass))
        return [{"candidate": f"refined@{current_pass}", "scores": {}}]

    _refine.calls = calls
    return _refine


class TestLoopWiring:
    def test_accept_on_first_pass_is_converged(self):
        evaluate = _scripted_evaluate([_verdict(ACCEPT, best_index=0, best_score=0.9)])
        refine = _recording_refine()
        out = run_optimizer_loop(
            [{"candidate": "x", "scores": {}}], evaluate=evaluate, refine=refine, max_passes=3
        )
        assert out["converged"] is True
        assert out["unconverged"] is False
        assert out["passes"] == 1
        assert out["verdict"] == ACCEPT
        assert out["best_score"] == 0.9
        # No refinement happened — accepted immediately.
        assert refine.calls == []

    def test_optimize_then_accept(self):
        evaluate = _scripted_evaluate(
            [_verdict(OPTIMIZE, best_index=0), _verdict(ACCEPT, best_index=0, best_score=0.8)]
        )
        refine = _recording_refine()
        out = run_optimizer_loop(
            [{"candidate": "p1", "scores": {}}], evaluate=evaluate, refine=refine, max_passes=3
        )
        assert out["converged"] is True
        assert out["passes"] == 2
        # refine ran exactly once (between pass 1 and pass 2).
        assert len(refine.calls) == 1
        # Pass 2 evaluated the REFINED candidate set, not the original.
        pass2_candidates = evaluate.calls[1][0]
        assert pass2_candidates[0]["candidate"] == "refined@1"

    def test_refine_receives_best_candidate(self):
        # best_index points at candidate 1 → refine must get THAT object.
        evaluate = _scripted_evaluate(
            [_verdict(OPTIMIZE, best_index=1), _verdict(ACCEPT, best_index=0)]
        )
        refine = _recording_refine()
        cands = [{"candidate": "a", "scores": {}}, {"candidate": "b", "scores": {}}]
        run_optimizer_loop(cands, evaluate=evaluate, refine=refine, max_passes=3)
        best_arg, pass_arg = refine.calls[0]
        assert best_arg["candidate"] == "b"  # the best (index 1), not index 0
        assert pass_arg == 1


class TestBoundedTermination:
    def test_stop_budget_returns_best_so_far_unconverged(self):
        # Evaluator never accepts; on the budget-exhausted pass decide() returns
        # STOP_BUDGET. The loop must stop and surface best-so-far as unconverged.
        evaluate = _scripted_evaluate(
            [_verdict(OPTIMIZE, best_index=0), _verdict(OPTIMIZE, best_index=0),
             _verdict(STOP_BUDGET, best_index=0, best_score=0.4)]
        )
        refine = _recording_refine()
        out = run_optimizer_loop(
            [{"candidate": "x", "scores": {}}], evaluate=evaluate, refine=refine, max_passes=3
        )
        assert out["converged"] is False
        assert out["unconverged"] is True
        assert out["verdict"] == STOP_BUDGET
        assert out["passes"] == 3
        assert out["best_score"] == 0.4  # best-so-far still surfaced

    def test_loop_runs_at_most_max_passes(self):
        # The HARD bound: an evaluator that returns OPTIMIZE forever must NOT
        # spin. With max_passes=3 the loop body runs exactly 3 times then stops
        # unconverged (independent ceiling, not relying on decide()'s own bound).
        evaluate = _scripted_evaluate([_verdict(OPTIMIZE, best_index=0)])  # always OPTIMIZE
        refine = _recording_refine()
        out = run_optimizer_loop(
            [{"candidate": "x", "scores": {}}], evaluate=evaluate, refine=refine, max_passes=3
        )
        assert out["converged"] is False
        assert out["passes"] == 3
        assert len(evaluate.calls) == 3  # evaluated 3 times, never a 4th
        # refine only fires BETWEEN passes → at most max_passes-1 times.
        assert len(refine.calls) == 2

    def test_max_passes_floored_to_one(self):
        # max_passes <= 0 is coerced to 1: a single evaluation, no refine.
        evaluate = _scripted_evaluate([_verdict(OPTIMIZE, best_index=0)])
        refine = _recording_refine()
        out = run_optimizer_loop(
            [{"candidate": "x", "scores": {}}], evaluate=evaluate, refine=refine, max_passes=0
        )
        assert out["max_passes"] == 1
        assert out["passes"] == 1
        assert len(evaluate.calls) == 1
        assert refine.calls == []  # never refines on a single-pass budget

    def test_default_max_passes_is_three(self):
        evaluate = _scripted_evaluate([_verdict(OPTIMIZE, best_index=0)])
        refine = _recording_refine()
        out = run_optimizer_loop(
            [{"candidate": "x", "scores": {}}], evaluate=evaluate, refine=refine
        )
        assert out["max_passes"] == DEFAULT_MAX_PASSES == 3
        assert len(evaluate.calls) == 3


class TestDeterminism:
    def test_same_inputs_same_output(self):
        def build():
            return run_optimizer_loop(
                [{"candidate": "x", "scores": {}}],
                evaluate=_scripted_evaluate(
                    [_verdict(OPTIMIZE, best_index=0), _verdict(ACCEPT, best_index=0, best_score=0.8)]
                ),
                refine=_recording_refine(),
                max_passes=3,
            )
        # Two independent runs with equivalent seams → identical outcome records.
        a, b = build(), build()
        assert a == b


class TestDefaultEvaluateWiring:
    def test_real_decide_drives_loop_to_accept(self):
        # End-to-end with the PRODUCTION evaluate seam (real evolution_evaluator
        # .decide), no stub: a fully-scored candidate above threshold ACCEPTs.
        good = {"candidate": "ok", "scores": {
            "relevance": 0.9, "evidence": 0.9, "specificity": 0.9, "correctness": 0.9}}
        evaluate = make_default_evaluate(threshold=0.75, max_passes=3)

        def refine(best, p):  # refinement that improves nothing (not exercised on accept)
            return [good]

        out = run_optimizer_loop([good], evaluate=evaluate, refine=refine, max_passes=3)
        assert out["converged"] is True
        assert out["passes"] == 1
        assert out["best_candidate"]["candidate"] == "ok"

    def test_real_decide_unscored_candidates_run_out_of_budget(self):
        # Empty scores → decide() can't judge → never ACCEPT. With a refine that
        # also yields unscored candidates, the loop walks the full budget and
        # stops unconverged. Proves the real gate + loop terminate together.
        unscored = {"candidate": "x", "scores": {}}
        evaluate = make_default_evaluate(threshold=0.75, max_passes=3)
        out = run_optimizer_loop(
            [unscored], evaluate=evaluate, refine=lambda best, p: [dict(unscored)], max_passes=3
        )
        assert out["converged"] is False
        assert out["verdict"] == STOP_BUDGET
        assert out["passes"] == 3

    def test_default_evaluate_does_not_invent_scores(self):
        # The production seam must NOT fabricate scores; it only runs decide()
        # over what the candidates already carry. An unscored candidate stays
        # unscored after evaluation.
        unscored = {"candidate": "x", "scores": {}}
        evaluate = make_default_evaluate(threshold=0.75, max_passes=3)
        scored, verdict = evaluate([unscored], 1)
        assert scored[0]["scores"] == {}
        assert verdict["best_score"] == 0.0


class TestCli:
    def test_converged_exit_code_and_json(self, tmp_path, capsys):
        p = tmp_path / "cands.json"
        p.write_text(json.dumps([{"candidate": "ok", "scores": {
            "relevance": 0.9, "evidence": 0.9, "specificity": 0.9, "correctness": 0.9}}]),
            encoding="utf-8")
        rc = main(["evolution_optimizer.py", "--threshold", "0.75", "--max-passes", "3", str(p)])
        payload = json.loads(capsys.readouterr().out)
        assert rc == EXIT_CONVERGED
        assert payload["converged"] is True
        assert payload["verdict"] == ACCEPT

    def test_unconverged_exit_code(self, tmp_path, capsys):
        # Unscored candidate → never accepts → STOP_BUDGET → exit 11.
        p = tmp_path / "cands.json"
        p.write_text(json.dumps([{"candidate": "x", "scores": {}}]), encoding="utf-8")
        rc = main(["evolution_optimizer.py", "--threshold", "0.75", "--max-passes", "3", str(p)])
        assert rc == EXIT_UNCONVERGED
        assert json.loads(capsys.readouterr().out)["unconverged"] is True

    def test_stdin_and_candidates_wrapper(self, monkeypatch, capsys):
        monkeypatch.setattr(
            sys, "stdin",
            io.StringIO(json.dumps({"candidates": [{"candidate": "ok", "scores": {
                "relevance": 0.9, "evidence": 0.9, "specificity": 0.9, "correctness": 0.9}}]})),
        )
        rc = main(["evolution_optimizer.py", "--threshold", "0.5"])
        assert rc == EXIT_CONVERGED
        assert json.loads(capsys.readouterr().out)["verdict"] == ACCEPT

    def test_bad_json_exit_2(self, tmp_path, capsys):
        p = tmp_path / "bad.json"
        p.write_text("{ not json", encoding="utf-8")
        rc = main(["evolution_optimizer.py", str(p)])
        assert rc == EXIT_BAD_INPUT
        assert "not valid JSON" in capsys.readouterr().err

    def test_non_list_payload_exit_2(self, tmp_path, capsys):
        p = tmp_path / "obj.json"
        p.write_text(json.dumps({"unexpected": "shape"}), encoding="utf-8")
        assert main(["evolution_optimizer.py", str(p)]) == EXIT_BAD_INPUT

    def test_unknown_flag_exit_2(self, capsys):
        assert main(["evolution_optimizer.py", "--bogus", "1"]) == EXIT_BAD_INPUT

    def test_bad_numeric_flag_exit_2(self, capsys):
        assert main(["evolution_optimizer.py", "--max-passes", "lots"]) == EXIT_BAD_INPUT
