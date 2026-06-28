#!/usr/bin/env python3
"""Evaluator-optimizer gate for the orchestrator-workers research loop (#230).

The orchestrator-workers + evaluator-optimizer workflow (Anthropic, "Building
Effective Agents") is: an orchestrator fans a research sub-task out to N worker
passes, each worker returns a candidate result, an EVALUATOR scores each
candidate against a small correctness rubric, and an OPTIMIZER decides whether
to ACCEPT the best one or run another refinement pass — bounded so the loop
always terminates (issue #230 success criterion: "the loop terminates within a
bounded number of evaluator passes").

The hard, repeatable part of that loop is NOT the LLM reasoning — it's the
DECISION LOGIC that keeps it honest and bounded. A model asked to grade its own
work converges to a false 10/10 and never stops (the same failure that motivated
the `evolution_skill_lint` gate). So this module makes the scoring + termination
DETERMINISTIC:

  * a fixed rubric of weighted criteria (each 0..1),
  * a single fused score per candidate (weighted mean of present criteria),
  * pick-the-best across candidates,
  * a verdict — ACCEPT (>= threshold), OPTIMIZE (below threshold, budget left),
    or STOP_BUDGET (below threshold, no passes left) — that an orchestrator
    follows verbatim instead of re-deciding with the model.

The LLM still does the open-ended work (decompose, run workers, write the
refinement). This module is the small deterministic referee that the skill
calls between passes. Pure functions + a thin CLI mirror the other
``scripts/evolution_*.py`` helpers so it is import-safe and unit-testable, and
the CLI gives the skill's terminal toolset a real call site (no dead code).

CLI (the skill calls this from its terminal tool with the candidates JSON on
stdin or a path):

    evolution_evaluator.py --threshold 0.75 --max-passes 3 candidates.json
    cat candidates.json | evolution_evaluator.py --threshold 0.75 --pass 1

It prints one JSON object: ``{"verdict", "best_index", "best_score", ...}`` and
exits 0 on ACCEPT, 10 on OPTIMIZE (run another pass), 11 on STOP_BUDGET, 2 on
bad input. The distinct exit codes let a shell loop branch without parsing JSON.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Default rubric for a research sub-task. Weights are relative (renormalized over
# whichever criteria a candidate actually scores), so an orchestrator can ship a
# partial rubric without skewing the scale. Kept deliberately small and generic —
# the skill MAY override per task, but these are sensible research defaults.
DEFAULT_RUBRIC: Dict[str, float] = {
    "relevance": 1.0,      # answers the actual sub-task, not an adjacent one
    "evidence": 1.0,       # claims are backed by cited sources, not asserted
    "specificity": 0.8,    # concrete + actionable vs. vague generality
    "correctness": 1.2,    # no detectable factual / logical errors
}

# Verdicts. ACCEPT = good enough, stop. OPTIMIZE = below bar but budget remains,
# run another refinement pass. STOP_BUDGET = below bar and out of passes; the
# orchestrator returns the best-so-far and flags it as unconverged.
ACCEPT = "ACCEPT"
OPTIMIZE = "OPTIMIZE"
STOP_BUDGET = "STOP_BUDGET"

# Exit codes for the shell-loop fast-path (distinct so a caller can branch on
# $? without parsing the JSON payload).
EXIT_ACCEPT = 0
EXIT_OPTIMIZE = 10
EXIT_STOP_BUDGET = 11
EXIT_BAD_INPUT = 2


def _clamp01(x: Any) -> float:
    """Coerce a score to a float in [0, 1]; non-numeric -> 0.0."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0.0
    if v < 0.0:
        return 0.0
    if v > 1.0:
        return 1.0
    return v


def score_candidate(scores: Dict[str, Any], rubric: Dict[str, float]) -> float:
    """Fuse a candidate's per-criterion scores into one [0, 1] number.

    Weighted mean over the rubric criteria the candidate actually scored. A
    missing criterion is OMITTED (not counted as 0) so a partial rubric does not
    artificially deflate the score — the weights renormalize over what's present.
    If the candidate scores NOTHING in the rubric, the result is 0.0 (it cannot
    be judged, so it cannot pass).
    """
    num = 0.0
    den = 0.0
    for crit, weight in rubric.items():
        if crit in scores:
            w = max(0.0, float(weight))
            num += w * _clamp01(scores[crit])
            den += w
    if den == 0.0:
        return 0.0
    return num / den


def evaluate_candidates(
    candidates: List[Dict[str, Any]],
    rubric: Optional[Dict[str, float]] = None,
) -> List[Tuple[int, float]]:
    """Score every candidate, returning ``(original_index, fused_score)`` pairs
    sorted best-first. Ties keep the earlier (lower-index) candidate first, so
    selection is stable and an earlier worker pass is preferred on a tie."""
    rubric = rubric or DEFAULT_RUBRIC
    scored: List[Tuple[int, float]] = []
    for i, cand in enumerate(candidates):
        cand_scores = cand.get("scores", {}) if isinstance(cand, dict) else {}
        if not isinstance(cand_scores, dict):
            cand_scores = {}
        scored.append((i, score_candidate(cand_scores, rubric)))
    # Sort by score desc, then original index asc (stable tie-break to earlier).
    scored.sort(key=lambda pair: (-pair[1], pair[0]))
    return scored


def decide(
    candidates: List[Dict[str, Any]],
    *,
    threshold: float = 0.75,
    current_pass: int = 1,
    max_passes: int = 3,
    rubric: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    """Run one evaluator-optimizer decision over the current pass's candidates.

    Returns a verdict record an orchestrator follows verbatim:

        {
          "verdict": ACCEPT | OPTIMIZE | STOP_BUDGET,
          "best_index": int | None,   # index into the input candidates list
          "best_score": float,        # fused rubric score of the best candidate
          "threshold": float,
          "pass": int,                # the pass this decision was made on
          "max_passes": int,
          "passes_remaining": int,    # how many optimize passes are still allowed
          "ranking": [[index, score], ...],  # all candidates, best-first
          "reason": str,              # human-readable one-liner
        }

    Termination is GUARANTEED: ``OPTIMIZE`` is only returned while
    ``current_pass < max_passes``; once the budget is spent the worst case is
    ``STOP_BUDGET`` (return best-so-far, flagged unconverged). With no candidates
    at all the verdict is OPTIMIZE if budget remains (let the orchestrator try
    again) else STOP_BUDGET — never a crash.
    """
    threshold = _clamp01(threshold)
    max_passes = max(1, int(max_passes))
    current_pass = max(1, int(current_pass))
    passes_remaining = max(0, max_passes - current_pass)
    has_budget = current_pass < max_passes

    ranking = evaluate_candidates(candidates, rubric)

    if not ranking:
        verdict = OPTIMIZE if has_budget else STOP_BUDGET
        reason = (
            "no candidates this pass; budget remains, run another"
            if has_budget
            else "no candidates and no passes left"
        )
        return {
            "verdict": verdict,
            "best_index": None,
            "best_score": 0.0,
            "threshold": threshold,
            "pass": current_pass,
            "max_passes": max_passes,
            "passes_remaining": passes_remaining,
            "ranking": [],
            "reason": reason,
        }

    best_index, best_score = ranking[0]

    if best_score >= threshold:
        verdict = ACCEPT
        reason = f"best score {best_score:.3f} >= threshold {threshold:.3f}"
    elif has_budget:
        verdict = OPTIMIZE
        reason = (
            f"best score {best_score:.3f} < threshold {threshold:.3f}; "
            f"{passes_remaining} pass(es) left — refine and re-evaluate"
        )
    else:
        verdict = STOP_BUDGET
        reason = (
            f"best score {best_score:.3f} < threshold {threshold:.3f} and "
            f"pass {current_pass}/{max_passes} exhausted — return best-so-far, unconverged"
        )

    return {
        "verdict": verdict,
        "best_index": best_index,
        "best_score": round(best_score, 6),
        "threshold": threshold,
        "pass": current_pass,
        "max_passes": max_passes,
        "passes_remaining": passes_remaining,
        "ranking": [[i, round(s, 6)] for i, s in ranking],
        "reason": reason,
    }


def _exit_code(verdict: str) -> int:
    return {
        ACCEPT: EXIT_ACCEPT,
        OPTIMIZE: EXIT_OPTIMIZE,
        STOP_BUDGET: EXIT_STOP_BUDGET,
    }.get(verdict, EXIT_BAD_INPUT)


def _parse_args(argv: List[str]) -> Tuple[Dict[str, Any], Optional[str]]:
    """Tiny hand-rolled arg parse (matches the other evolution_* CLIs' style).

    Returns (opts, error). ``opts`` carries threshold/current_pass/max_passes and
    an optional ``path`` (positional; absent -> read stdin)."""
    opts: Dict[str, Any] = {
        "threshold": 0.75,
        "current_pass": 1,
        "max_passes": 3,
        "path": None,
    }
    i = 0
    rest = argv[1:]
    while i < len(rest):
        arg = rest[i]
        if arg in ("--threshold", "--pass", "--max-passes"):
            if i + 1 >= len(rest):
                return opts, f"{arg} needs a value"
            val = rest[i + 1]
            try:
                if arg == "--threshold":
                    opts["threshold"] = float(val)
                elif arg == "--pass":
                    opts["current_pass"] = int(val)
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


def _load_candidates(opts: Dict[str, Any]) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    """Read the candidates payload from the positional path or stdin.

    Accepts either a bare JSON list of candidate objects, or an object with a
    ``"candidates"`` key (so a richer orchestrator payload also works)."""
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
        return None, "expected a JSON list of candidates (or {\"candidates\": [...]})"
    return [c if isinstance(c, dict) else {} for c in data], None


def main(argv: List[str]) -> int:
    opts, err = _parse_args(argv)
    if err:
        print(f"[evolution-evaluator] {err}", file=sys.stderr)
        return EXIT_BAD_INPUT
    candidates, load_err = _load_candidates(opts)
    if load_err:
        print(f"[evolution-evaluator] {load_err}", file=sys.stderr)
        return EXIT_BAD_INPUT
    result = decide(
        candidates,
        threshold=opts["threshold"],
        current_pass=opts["current_pass"],
        max_passes=opts["max_passes"],
    )
    print(json.dumps(result, sort_keys=True))
    return _exit_code(result["verdict"])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
