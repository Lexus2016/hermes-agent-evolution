#!/usr/bin/env python3
"""Bounded optimizer loop for the orchestrator-workers research loop (#301).

The orchestrator-workers + evaluator-optimizer workflow (Anthropic, "Building
Effective Agents") is: an ORCHESTRATOR fans a research sub-task out to N WORKERS
(``scripts/evolution_orchestrator.py`` owns that fan-out + collection), an
EVALUATOR scores the N candidates against a rubric, and an OPTIMIZER decides
whether to ACCEPT the best, OPTIMIZE (refine the best and run another pass), or
STOP_BUDGET (out of passes → return best-so-far flagged ``unconverged``).

The evaluator gate already shipped: ``scripts/evolution_evaluator.decide`` is the
deterministic referee that returns ``ACCEPT | OPTIMIZE | STOP_BUDGET`` plus the
best candidate index/score, and is bounded so it never emits ``OPTIMIZE`` once
the pass budget is spent. This module is the remaining slice (#301): the LOOP
that feeds candidates through ``decide`` and acts on its verdict —

  * on ``ACCEPT``  → done, ``converged=True``;
  * on ``OPTIMIZE`` → call ``refine`` on the best candidate to produce the next
    pass's candidate set, increment the pass counter, re-evaluate;
  * on ``STOP_BUDGET`` → done, ``converged=False`` (best-so-far, ``unconverged``).

Termination is GUARANTEED by ``decide`` (no ``OPTIMIZE`` once
``current_pass == max_passes``) AND independently re-asserted here by the pass
ceiling: the loop body runs at most ``max_passes`` times, so even a misbehaving
injected ``evaluate`` cannot make it spin. ``max_passes`` is hard-floored to 1
and the issue's default is 3.

The two open-ended steps are INJECTED SEAMS so the loop is deterministic and
testable with no network / no model:

  * ``evaluate(candidates, current_pass) -> (scored_candidates, verdict_record)``
    — the EVALUATOR. It scores the candidates (fills each ``"scores"`` dict) and
    returns ``decide``'s verdict record for the pass. ``default_evaluate`` below
    is the production wiring: it trusts candidates that already carry ``scores``
    (an LLM judge filled them) and calls ``evolution_evaluator.decide``. Tests
    inject a stub that returns canned verdicts — no model needed.
  * ``refine(best_candidate, current_pass) -> next_candidates`` — the OPTIMIZER's
    refinement step (itself an LLM / delegate action). It takes the best
    candidate of the current pass and returns the NEXT pass's candidate list.
    This is the only network-touching step in production; behind the seam, the
    loop wiring is fully unit-testable. The DELIVERABLE of #301 is the bounded
    loop wiring, NOT the refiner — so ``refine`` stays injected and this module
    ships no real refiner.

SCOPE BOUNDARY: this is the loop-wiring slice ONLY. Scoring lives in
``evolution_evaluator`` (reused, never reimplemented here); fan-out + collection
live in ``evolution_orchestrator``. The actual refinement LLM call is the
injected ``refine`` seam, deliberately not implemented here.

CLI (mirrors the sibling ``evolution_*.py`` helpers — pure functions + a thin
hand-rolled CLI, import-safe and unit-testable). The CLI runs the loop with the
NON-LLM seams: candidates are scored from the ``scores`` they already carry, and
``refine`` is a no-op pass-through (the terminal toolset has no model to call),
so a single-pass run is the useful shell call site —

    evolution_optimizer.py --threshold 0.75 --max-passes 3 candidates.json
    cat candidates.json | evolution_optimizer.py --threshold 0.75

It prints one JSON object
``{"converged", "passes", "best_index", "best_score", "verdict", ...}`` and exits
0 when converged (ACCEPT), 11 when it stops unconverged (STOP_BUDGET), 2 on bad
input — matching the evaluator's exit-code convention so a shell loop can branch
on ``$?`` without parsing JSON.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# Reuse the shipped evaluator gate — scoring + the bounded ACCEPT/OPTIMIZE/
# STOP_BUDGET verdict are NOT reimplemented here.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from evolution_evaluator import (  # noqa: E402
    ACCEPT,
    OPTIMIZE,
    STOP_BUDGET,
    decide,
)

DEFAULT_MAX_PASSES = 3
DEFAULT_THRESHOLD = 0.75

# Exit codes mirror evolution_evaluator (converged ↔ ACCEPT, unconverged ↔
# STOP_BUDGET) so a shell caller branches on $? identically across both tools.
EXIT_CONVERGED = 0
EXIT_UNCONVERGED = 11
EXIT_BAD_INPUT = 2

# Seam type aliases (documentation only — kept loose so stubs/lambdas slot in).
EvaluateFn = Callable[[List[Dict[str, Any]], int], Tuple[List[Dict[str, Any]], Dict[str, Any]]]
RefineFn = Callable[[Optional[Dict[str, Any]], int], List[Dict[str, Any]]]


def make_default_evaluate(
    *,
    threshold: float = DEFAULT_THRESHOLD,
    max_passes: int = DEFAULT_MAX_PASSES,
    rubric: Optional[Dict[str, float]] = None,
) -> EvaluateFn:
    """Build the PRODUCTION ``evaluate`` seam over ``evolution_evaluator.decide``.

    The returned callable takes ``(candidates, current_pass)`` and returns
    ``(candidates, verdict_record)``. It does NOT invent scores: candidates are
    expected to already carry their per-criterion ``"scores"`` (filled by an LLM
    judge upstream, or by the fixture in a test). It only runs the deterministic
    ``decide`` gate over them — so scoring stays the evaluator's job and this
    module never fakes a pass. The candidates are returned unchanged alongside
    the verdict so ``run_optimizer_loop`` can index the best one for ``refine``.
    """

    def _evaluate(
        candidates: List[Dict[str, Any]], current_pass: int
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        verdict = decide(
            candidates,
            threshold=threshold,
            current_pass=current_pass,
            max_passes=max_passes,
            rubric=rubric,
        )
        return candidates, verdict

    return _evaluate


def _best_candidate(
    candidates: List[Dict[str, Any]], best_index: Optional[int]
) -> Optional[Dict[str, Any]]:
    """Safely fetch the best candidate by index; ``None`` if absent/out of range."""
    if best_index is None:
        return None
    if 0 <= best_index < len(candidates):
        return candidates[best_index]
    return None


def run_optimizer_loop(
    candidates: List[Dict[str, Any]],
    *,
    evaluate: EvaluateFn,
    refine: RefineFn,
    max_passes: int = DEFAULT_MAX_PASSES,
) -> Dict[str, Any]:
    """Run the bounded evaluator-optimizer loop and return its outcome.

    Starting from the first pass's ``candidates``, on each pass:

      1. ``evaluate(candidates, current_pass)`` scores them and returns the
         ``decide`` verdict record for the pass.
      2. branch on ``verdict["verdict"]``:
         * ``ACCEPT``     → stop, ``converged=True``.
         * ``STOP_BUDGET`` → stop, ``converged=False`` (best-so-far, unconverged).
         * ``OPTIMIZE``   → ``refine`` the best candidate into the next pass's
           candidate set, ``current_pass += 1``, repeat.

    Determinism: given the same injected ``evaluate``/``refine`` and inputs, the
    return is identical — no randomness, no clock, no IO.

    Bounded termination (issue #301 / parent #230 success criterion): the loop
    body executes AT MOST ``max_passes`` times. ``decide`` already refuses to
    emit ``OPTIMIZE`` once ``current_pass == max_passes``, but this function does
    NOT rely on that alone — the ``while current_pass <= max_passes`` ceiling is
    an independent guard, so a misbehaving injected ``evaluate`` that returns
    ``OPTIMIZE`` forever still terminates (falling out of the loop as
    unconverged). ``max_passes`` is hard-floored to 1.

    Returns::

        {
          "converged": bool,          # True iff a pass ACCEPTed
          "passes": int,              # number of passes actually run (>= 1)
          "max_passes": int,
          "verdict": str,             # terminal verdict (ACCEPT/STOP_BUDGET/…)
          "best_index": int | None,   # index into the FINAL pass's candidates
          "best_score": float,
          "best_candidate": dict | None,  # the winning candidate object
          "unconverged": bool,        # convenience negation of converged
          "decision": dict,           # the full terminal decide() record
        }
    """
    max_passes = max(1, int(max_passes))
    current_pass = 1
    last_candidates: List[Dict[str, Any]] = list(candidates)
    last_decision: Dict[str, Any] = {}

    while current_pass <= max_passes:
        scored, decision = evaluate(last_candidates, current_pass)
        last_candidates = scored
        last_decision = decision
        verdict = decision.get("verdict")

        if verdict == ACCEPT:
            return _result(True, current_pass, max_passes, scored, decision)
        if verdict == STOP_BUDGET:
            return _result(False, current_pass, max_passes, scored, decision)
        # OPTIMIZE (or any non-terminal verdict): refine the best and run again,
        # if the pass ceiling still allows it.
        if current_pass >= max_passes:
            break
        best = _best_candidate(scored, decision.get("best_index"))
        last_candidates = list(refine(best, current_pass))
        current_pass += 1

    # Fell out of the loop without a terminal ACCEPT — treat as unconverged
    # (out of passes). This is the safety net for a misbehaving ``evaluate`` that
    # never emits a terminal verdict; ``decide`` itself would have returned
    # STOP_BUDGET on the final pass.
    return _result(False, current_pass, max_passes, last_candidates, last_decision)


def _result(
    converged: bool,
    passes: int,
    max_passes: int,
    candidates: List[Dict[str, Any]],
    decision: Dict[str, Any],
) -> Dict[str, Any]:
    """Assemble the loop's outcome record."""
    best_index = decision.get("best_index")
    return {
        "converged": converged,
        "unconverged": not converged,
        "passes": passes,
        "max_passes": max_passes,
        "verdict": decision.get("verdict"),
        "best_index": best_index,
        "best_score": decision.get("best_score", 0.0),
        "best_candidate": _best_candidate(candidates, best_index),
        "decision": decision,
    }


# ── CLI (non-LLM seams: score from carried ``scores``, refine is a no-op) ────────
def _noop_refine(best: Optional[Dict[str, Any]], current_pass: int) -> List[Dict[str, Any]]:
    """CLI refinement seam: the terminal toolset has no model to call, so the
    'refinement' is the best candidate carried forward unchanged. The loop then
    re-evaluates it (same scores → same verdict) and the pass budget terminates
    it. The CLI is therefore effectively a single useful evaluation pass; the
    real multi-pass loop runs in-process with an LLM-backed ``refine`` injected."""
    return [best] if best is not None else []


def _parse_args(argv: List[str]) -> Tuple[Dict[str, Any], Optional[str]]:
    """Hand-rolled arg parse matching the sibling evolution_* CLIs' style."""
    opts: Dict[str, Any] = {
        "threshold": DEFAULT_THRESHOLD,
        "max_passes": DEFAULT_MAX_PASSES,
        "path": None,
    }
    rest = argv[1:]
    i = 0
    while i < len(rest):
        arg = rest[i]
        if arg in ("--threshold", "--max-passes"):
            if i + 1 >= len(rest):
                return opts, f"{arg} needs a value"
            val = rest[i + 1]
            try:
                if arg == "--threshold":
                    opts["threshold"] = float(val)
                else:
                    opts["max_passes"] = int(val)
            except ValueError:
                return opts, f"{arg} value must be a number, got {val!r}"
            i += 2
            continue
        if arg.startswith("-"):
            return opts, f"unknown flag: {arg}"
        opts["path"] = arg
        i += 1
    return opts, None


def _load_candidates(
    opts: Dict[str, Any],
) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    """Read candidates from the positional path or stdin. Accepts a bare JSON
    list or an object with a ``"candidates"`` key (the orchestrator's payload)."""
    path = opts.get("path")
    try:
        raw = Path(path).read_text(encoding="utf-8") if path else sys.stdin.read()
    except OSError as exc:
        return None, f"cannot read input: {exc}"
    try:
        data = json.loads(raw)
    except ValueError as exc:
        return None, f"input is not valid JSON: {exc}"
    if isinstance(data, dict) and "candidates" in data:
        data = data["candidates"]
    if not isinstance(data, list):
        return None, 'expected a JSON list of candidates (or {"candidates": [...]})'
    return [c if isinstance(c, dict) else {} for c in data], None


def main(argv: List[str]) -> int:
    opts, err = _parse_args(argv)
    if err:
        print(f"[evolution-optimizer] {err}", file=sys.stderr)
        return EXIT_BAD_INPUT
    candidates, load_err = _load_candidates(opts)
    if load_err:
        print(f"[evolution-optimizer] {load_err}", file=sys.stderr)
        return EXIT_BAD_INPUT
    evaluate = make_default_evaluate(
        threshold=opts["threshold"], max_passes=opts["max_passes"]
    )
    result = run_optimizer_loop(
        candidates,
        evaluate=evaluate,
        refine=_noop_refine,
        max_passes=opts["max_passes"],
    )
    print(json.dumps(result, sort_keys=True))
    return EXIT_CONVERGED if result["converged"] else EXIT_UNCONVERGED


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
