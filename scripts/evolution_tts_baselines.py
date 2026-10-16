#!/usr/bin/env python3
"""Naive test-time-scaling (TTS) baselines for evolution evaluation (#41).

Recent research (arXiv 2607.12227) shows that *naive* test-time scaling —
letting an agent take more sampling iterations, higher temperature passes, or
best-of-N independent attempts — can match or exceed complex evolved harnesses
on many benchmarks. If that is true, an evolution pipeline that measures only
"evolved vs. non-evolved" is measuring against the wrong yardstick: it can
report an improvement that a trivial baseline would have matched for free.

This module gives the evaluator that missing comparison arm. It is purely
deterministic arithmetic over per-attempt scores:

* ``best_of_n`` — the classic TTS arm: N independent attempts, keep the best.
* ``pass_at_k`` — probability that at least one of k attempts clears the
  acceptance threshold (useful when each attempt is pass/fail against a
  rubric bar rather than continuously scored).
* ``baseline_curve`` — best-of-N score as N grows, so a harness can see where
  naive scaling plateaus.
* ``compare_against_baseline`` — the verdict the evaluator actually wants:
  does the evolved harness beat the naive best-of-N baseline, and by how much,
  and with what margin?

Design mirrors the other ``scripts/evolution_*.py`` helpers: pure functions +
a thin CLI, no LLM, no network. The CLI is the real call site exercised by
``tests/scripts/test_evolution_tts_baselines.py``, and
``evolution_benchmark.py baselines`` wraps it so a benchmark run can report
the TTS comparison in the same output it already prints.

CLI::

    evolution_tts_baselines.py --evolved 0.91 --samples 0.6 0.7 0.82 0.9 --threshold 0.75
    cat scores.json | evolution_tts_baselines.py --evolved 0.91 --threshold 0.75

Prints one JSON object with the verdict and exits 0; 2 on bad input.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Dict, List, Optional

__all__ = [
    "baseline_curve",
    "best_of_n",
    "compare_against_baseline",
    "main",
    "pass_at_k",
]

#: A harness that merely *ties* the naive baseline is not an improvement —
#: the baseline cost nothing to obtain. The evolved harness must clear the
#: best-of-N score by at least this absolute margin to be reported as
#: ``BEATS_BASELINE``.
DEFAULT_MARGIN = 0.02


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


def _sample_list(samples: Any) -> list[float]:
    """Coerce the samples argument to a list of [0,1] floats.

    Accepts a JSON list, a whitespace/comma-separated string, or a list of
    numbers. Non-numeric entries are dropped rather than crashing the
    comparison — a malformed sample is noise, not a verdict.
    """
    if samples is None:
        return []
    if isinstance(samples, str):
        samples = samples.replace(",", " ").split()
    if not isinstance(samples, (list, tuple)):
        return []
    out: list[float] = []
    for s in samples:
        if isinstance(s, (dict, list)):
            continue
        try:
            out.append(_clamp01(s))
        except (TypeError, ValueError):
            continue
    return out


def best_of_n(scores: list[float]) -> float:
    """Best-of-N test-time-scaling arm: the maximum of N attempt scores.

    The naive ceiling: an orchestrator that simply re-runs the task N times
    with temperature/perturbation and keeps the best result scores this.
    Empty input -> 0.0 (no attempt succeeded or was recorded).
    """
    return max(scores) if scores else 0.0


def pass_at_k(scores: list[float], *, threshold: float = 0.75) -> float:
    """Probability that at least one of k attempts clears ``threshold``.

    Each attempt is treated as an independent Bernoulli trial with observed
    pass rate ``p = (#scores >= threshold) / N``; the estimate is
    ``1 - (1 - p)^N`` for the observed N. With no samples the probability is
    0.0 (nothing was tried, so nothing can pass).
    """
    threshold = _clamp01(threshold)
    n = len(scores)
    if n == 0:
        return 0.0
    passed = sum(1 for s in scores if s >= threshold)
    p = passed / n
    return 1.0 - (1.0 - p) ** n


def baseline_curve(scores: list[float], *, max_n: int = 8) -> list[dict[str, float]]:
    """Best-of-N curve: the baseline score achievable at each attempt budget.

    ``curve[i] = best_of_n(scores[:i+1])`` — the prefix-max of sorted-descending
    attempts. Because best-of-N is monotone non-decreasing in N, this is the
    honest statement of what naive scaling buys: it shows the plateau where
    extra attempts stop helping. ``max_n`` bounds the curve (default 8, the
    pragmatic sampling budget for an evaluation run).
    """
    ordered = sorted(scores, reverse=True)
    curve: list[dict[str, float]] = []
    for i in range(min(max(0, max_n), len(ordered))):
        curve.append({"n": i + 1, "best": round(best_of_n(ordered[: i + 1]), 6)})
    return curve


def compare_against_baseline(
    evolved_score: Any,
    samples: Any,
    *,
    threshold: float = 0.75,
    margin: float = DEFAULT_MARGIN,
) -> dict[str, Any]:
    """Compare an evolved harness score against the naive best-of-N baseline.

    Verdict semantics:

    * ``BEATS_BASELINE`` — evolved score clears the baseline best-of-N by at
      least ``margin``. The harness earns its keep.
    * ``TIES_BASELINE`` — within ``margin`` of the baseline. Naive scaling
      would have bought the same result: not an improvement worth claiming.
    * ``LOSES_TO_BASELINE`` — strictly below the baseline minus margin. The
      evolved harness is actively worse than doing nothing clever.
    * ``NO_BASELINE`` — no attempt scores were supplied. No comparison
      possible (the evolved score alone cannot be validated).

    Returns a verdict record an evaluator can embed in its report::

        {
          "verdict": ...,
          "evolved_score": float,
          "best_of_n": float,
          "n_samples": int,
          "pass_at_k": float,
          "threshold": float,
          "margin": float,
          "curve": [{"n": 1, "best": ...}, ...],
        }
    """
    evolved = _clamp01(evolved_score)
    threshold = _clamp01(threshold)
    margin = max(0.0, float(margin))
    scores = _sample_list(samples)
    n = len(scores)

    if n == 0:
        return {
            "verdict": "NO_BASELINE",
            "evolved_score": evolved,
            "best_of_n": 0.0,
            "n_samples": 0,
            "pass_at_k": 0.0,
            "threshold": threshold,
            "margin": margin,
            "curve": [],
        }

    best = best_of_n(scores)
    pas = pass_at_k(scores, threshold=threshold)
    if evolved >= best + margin:
        verdict = "BEATS_BASELINE"
    elif evolved >= best - margin:
        verdict = "TIES_BASELINE"
    else:
        verdict = "LOSES_TO_BASELINE"

    return {
        "verdict": verdict,
        "evolved_score": evolved,
        "best_of_n": round(best, 6),
        "n_samples": n,
        "pass_at_k": round(pas, 6),
        "threshold": threshold,
        "margin": margin,
        "curve": baseline_curve(scores),
    }


def _parse_args(argv: list[str]) -> tuple[dict[str, Any], Optional[str]]:
    """Tiny hand-rolled arg parse (matches the other evolution_* CLIs' style).

    Flags: ``--evolved``, ``--threshold``, ``--margin``, ``--max-n``.
    Samples come from either repeated ``--sample`` flags, a positional path
    to a JSON file, or stdin (a JSON list)."""
    opts: dict[str, Any] = {
        "evolved": None,
        "threshold": 0.75,
        "margin": DEFAULT_MARGIN,
        "max_n": 8,
        "samples": [],
        "path": None,
    }
    i = 0
    rest = argv[1:]
    while i < len(rest):
        arg = rest[i]
        if arg in ("--evolved", "--threshold", "--margin", "--max-n", "--sample"):
            if i + 1 >= len(rest):
                return opts, f"{arg} needs a value"
            val = rest[i + 1]
            if arg == "--sample":
                opts["samples"].append(val)
            else:
                try:
                    if arg == "--evolved":
                        opts["evolved"] = float(val)
                    elif arg == "--threshold":
                        opts["threshold"] = float(val)
                    elif arg == "--margin":
                        opts["margin"] = float(val)
                    else:
                        opts["max_n"] = int(val)
                except ValueError:
                    return opts, f"{arg} value must be a number, got {val!r}"
            i += 2
            continue
        if arg.startswith("-"):
            return opts, f"unknown flag: {arg}"
        opts["path"] = arg
        i += 1
    return opts, None


def _load_samples(opts: dict[str, Any]) -> tuple[list[float], Optional[str]]:
    """Read attempt scores from ``--sample`` flags, a path, or stdin.

    A positional path may hold either a bare JSON list of numbers or an object
    with a ``\"samples\"`` key (so a richer harness output also works)."""
    if opts["samples"]:
        return _sample_list(opts["samples"]), None
    path = opts.get("path")
    if path is None:
        raw = sys.stdin.read()
    else:
        from pathlib import Path

        try:
            raw = Path(path).read_text(encoding="utf-8-sig")
        except OSError as exc:
            return [], f"cannot read input: {exc}"
    try:
        data = json.loads(raw)
    except ValueError as exc:
        return [], f"input is not valid JSON: {exc}"
    if isinstance(data, dict) and "samples" in data:
        data = data["samples"]
    return _sample_list(data), None


def main(argv: list[str]) -> int:
    opts, err = _parse_args(argv)
    if err:
        print(f"[evolution-tts-baselines] {err}", file=sys.stderr)
        return 2
    if opts["evolved"] is None:
        print("[evolution-tts-baselines] --evolved is required", file=sys.stderr)
        return 2
    samples, load_err = _load_samples(opts)
    if load_err:
        print(f"[evolution-tts-baselines] {load_err}", file=sys.stderr)
        return 2
    result = compare_against_baseline(
        opts["evolved"],
        samples,
        threshold=opts["threshold"],
        margin=opts["margin"],
    )
    # Bounded curve length comes from the CLI's --max-n, not the default.
    if opts["max_n"]:
        result["curve"] = baseline_curve(samples, max_n=opts["max_n"])
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
