#!/usr/bin/env python3
"""Tool-use competency rubric over real agent traces (issue #1268).

Five diagnostic dimensions from MCP-Atlas (arXiv:2602.00933), scored over the
tool-call trajectories that ``agent/tool_call_capture.py`` records since #1363:

  discovery        did the agent find the right tool, or reformulate searches?
  parameterization were argument shapes right, or retried after malformed args?
  syntax           were the calls well-formed at all?
  error_recovery   after a failure, a different approach or an identical retry?
  efficiency       how much of the work was distinct rather than redundant?

Written against the owner's rework brief on the two previous attempts, both of
which were closed as incoherent. Quoting the verdict on PR #1281, because every
clause of it is a requirement here:

    "The rubric reads the funnel's own synthetic trajectory record rather than
    real agent tool calls, then flattens independent sessions and assigns every
    call turn 0. That makes the diagnostic unable to measure the claimed
    competency dimensions and creates false repeated-call clusters. [...] it
    needs a future implementation that extracts privacy-safe real traces from
    session storage, preserves session and turn boundaries, evaluates each
    trace separately, and lets the funnel consume only the resulting summary."

So, concretely:

* **Real traces.** Reads ``<evolution_dir>/trajectories/*.jsonl`` — the #1363
  capture of actual agent tool calls — and ignores the single-entry
  ``<date>.json`` files the funnel writes about itself.
* **Boundaries preserved.** One JSONL line is one turn; the filename carries
  the session. Turns are never concatenated across sessions, which is what
  manufactured the false repeated-call clusters last time.
* **Scored per trace.** Each turn is scored on its own; the corpus figure is an
  average of per-turn scores, not a score over a merged pile of calls.
* **Summary only.** ``score_corpus`` returns per-dimension averages and counts.
  The funnel consumes that, never the traces.

Privacy is inherited rather than re-solved: the captured entries already hold
redacted argument summaries and truncated result summaries, never user prose.

Deterministic — no LLM, no network. Pure functions plus a thin CLI.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

SCHEMA_VERSION = "1"

DIMENSIONS = (
    "discovery",
    "parameterization",
    "syntax",
    "error_recovery",
    "efficiency",
)

#: Tools whose repeated use *is* the discovery process. Reformulating a search
#: several times before invoking anything is the #1144 signal.
_DISCOVERY_TOOLS = frozenset({"tool_search", "tool_describe"})
_DISCOVERY_TOLERANCE = 2

#: A failed call followed by an identical one is the "infinite debugging loop"
#: LHTB names — the cheapest and highest-value error-recovery signal, per the
#: issue.
_IDENTICAL_RETRY_PENALTY = 0.5

#: Syntax failures are parse-level: the call could not even be formed.
_PARSE_ERROR_MARKERS = ("parse error", "invalid json", "jsondecode", "unexpected token")


@dataclass
class TurnScore:
    """Per-dimension scores for ONE turn, plus what drove them."""

    session_id: str = ""
    turn_index: int = 0
    n_calls: int = 0
    scores: Dict[str, float] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)

    @property
    def overall(self) -> float:
        if not self.scores:
            return 0.0
        return round(sum(self.scores.values()) / len(self.scores), 4)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "turn_index": self.turn_index,
            "n_calls": self.n_calls,
            "scores": dict(self.scores),
            "overall": self.overall,
            "notes": list(self.notes),
        }


def _call_signature(entry: Dict[str, Any]) -> str:
    """Identity of a call for redundancy purposes: tool + its argument shape."""
    args = entry.get("args_summary")
    try:
        args_repr = json.dumps(args, sort_keys=True) if isinstance(args, dict) else str(args)
    except (TypeError, ValueError):
        args_repr = str(args)
    return f"{entry.get('tool', '')}::{args_repr}"


def _failed(entry: Dict[str, Any]) -> bool:
    return str(entry.get("result_status", "")).lower() in ("failure", "error")


def _looks_like_parse_error(entry: Dict[str, Any]) -> bool:
    blob = f"{entry.get('result_summary', '')}".lower()
    return any(marker in blob for marker in _PARSE_ERROR_MARKERS)


def score_turn(
    entries: List[Dict[str, Any]],
    *,
    session_id: str = "",
    turn_index: int = 0,
) -> TurnScore:
    """Score ONE turn's tool calls across the five dimensions.

    Every dimension is 0.0–1.0 where 1.0 is competent. A turn with no calls
    returns an empty score rather than a perfect one — nothing was attempted,
    so there is nothing to be competent at, and averaging in a free 1.0 would
    flatter the corpus figure.
    """
    score = TurnScore(session_id=session_id, turn_index=turn_index, n_calls=len(entries))
    if not entries:
        return score

    total = len(entries)
    signatures = [_call_signature(e) for e in entries]
    failures = [e for e in entries if _failed(e)]

    # -- discovery: searching is fine; searching instead of acting is not.
    discovery_calls = sum(
        1 for e in entries if str(e.get("tool", "")) in _DISCOVERY_TOOLS
    )
    if discovery_calls:
        excess = max(0, discovery_calls - _DISCOVERY_TOLERANCE)
        score.scores["discovery"] = round(max(0.0, 1.0 - excess / max(1, total)), 4)
        if excess:
            score.notes.append(
                f"{discovery_calls} discovery calls ({excess} beyond tolerance)"
            )
    else:
        score.scores["discovery"] = 1.0

    # -- parameterization: a failure followed by the SAME tool with DIFFERENT
    #    args is the model correcting an argument shape. Frequent correction
    #    means the first shape was wrong.
    corrections = 0
    for i, entry in enumerate(entries[:-1]):
        if not _failed(entry):
            continue
        nxt = entries[i + 1]
        if entry.get("tool") == nxt.get("tool") and signatures[i] != signatures[i + 1]:
            corrections += 1
    score.scores["parameterization"] = round(max(0.0, 1.0 - corrections / total), 4)
    if corrections:
        score.notes.append(f"{corrections} argument correction(s) after failure")

    # -- syntax: calls that could not be formed at all.
    parse_errors = sum(1 for e in entries if _looks_like_parse_error(e))
    score.scores["syntax"] = round(max(0.0, 1.0 - parse_errors / total), 4)
    if parse_errors:
        score.notes.append(f"{parse_errors} parse-level failure(s)")

    # -- error_recovery: after a failure, did the agent change anything?
    #    An identical re-issue is the infinite-debugging-loop shape.
    if failures:
        identical_retries = 0
        for i, entry in enumerate(entries[:-1]):
            if _failed(entry) and signatures[i] == signatures[i + 1]:
                identical_retries += 1
        penalty = min(1.0, identical_retries * _IDENTICAL_RETRY_PENALTY)
        score.scores["error_recovery"] = round(max(0.0, 1.0 - penalty), 4)
        if identical_retries:
            score.notes.append(f"{identical_retries} identical retry after failure")
    else:
        score.scores["error_recovery"] = 1.0

    # -- efficiency: share of calls that were distinct work.
    score.scores["efficiency"] = round(len(set(signatures)) / total, 4)
    if len(set(signatures)) < total:
        score.notes.append(f"{total - len(set(signatures))} redundant call(s)")

    return score


def load_turns(trajectory_dir: Path) -> List[Dict[str, Any]]:
    """Read captured turns, preserving session and turn boundaries.

    Reads only the ``.jsonl`` files written by the #1363 capture. The funnel's
    own ``<date>.json`` files describe the pipeline's invocation of itself and
    are deliberately skipped — scoring them was the core defect in the previous
    attempt.
    """
    turns: List[Dict[str, Any]] = []
    if not trajectory_dir.is_dir():
        return turns
    for path in sorted(trajectory_dir.glob("*.jsonl")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for idx, line in enumerate(lines):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(rec, dict) or not isinstance(rec.get("entries"), list):
                continue
            rec["_turn_index"] = idx
            turns.append(rec)
    return turns


def score_corpus(turns: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Average per-turn scores into the summary the funnel consumes.

    Turns are scored individually and then averaged — never merged into one
    pile of calls, which is what produced false repeated-call clusters before.
    """
    per_turn = [
        score_turn(
            t.get("entries", []),
            session_id=str(t.get("session_id", "")),
            turn_index=int(t.get("_turn_index", 0)),
        )
        for t in turns
    ]
    scored = [s for s in per_turn if s.n_calls]

    dims: Dict[str, Optional[float]] = {}
    for dim in DIMENSIONS:
        values = [s.scores[dim] for s in scored if dim in s.scores]
        dims[dim] = round(sum(values) / len(values), 4) if values else None

    graded = [d for d in dims.values() if d is not None]
    return {
        "schema_version": SCHEMA_VERSION,
        "turns_scored": len(scored),
        "turns_seen": len(per_turn),
        "sessions": len({s.session_id for s in scored if s.session_id}),
        "total_calls": sum(s.n_calls for s in scored),
        "dimensions": dims,
        "overall": round(sum(graded) / len(graded), 4) if graded else None,
    }


def format_summary(summary: Dict[str, Any]) -> str:
    """One line for the evolution-health sidecar / a log."""
    if not summary.get("turns_scored"):
        return "[tooluse-rubric] no captured turns to score"
    parts = [
        f"{dim}={summary['dimensions'][dim]:.0%}"
        for dim in DIMENSIONS
        if summary["dimensions"].get(dim) is not None
    ]
    return (
        f"[tooluse-rubric] {summary['turns_scored']} turns / "
        f"{summary['sessions']} sessions / {summary['total_calls']} calls: "
        + " ".join(parts)
    )


def _default_trajectory_dir() -> Path:
    env = os.environ.get("EVOLUTION_PROFILE_DIR", "").strip()
    if env:
        return Path(env) / "trajectories"
    hh = os.environ.get("HERMES_HOME", "").strip()
    return (
        Path(hh) / "evolution" / "trajectories"
        if hh
        else Path.home() / ".hermes" / "evolution" / "trajectories"
    )


def main(argv: Optional[List[str]] = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if "--help" in args or "-h" in args:
        print(
            "usage: evolution_tooluse_rubric.py [--trajectory-dir DIR]\n"
            "  Scores captured agent tool calls (#1363) across the five\n"
            "  MCP-Atlas competency dimensions. JSON summary to stdout.\n"
            "  Exit 0 always; 2 on bad arguments."
        )
        return 0

    trajectory_dir = _default_trajectory_dir()
    if "--trajectory-dir" in args:
        i = args.index("--trajectory-dir")
        try:
            trajectory_dir = Path(args[i + 1])
        except IndexError:
            print("error: --trajectory-dir needs a path", file=sys.stderr)
            return 2

    summary = score_corpus(load_turns(trajectory_dir))
    print(json.dumps(summary, indent=2, sort_keys=True))
    print(format_summary(summary), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
