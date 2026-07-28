#!/usr/bin/env python3
"""Retrieve the heuristics worth injecting for a task (issue #1360).

Child B of #1303 (ERL). Slice A (#1359) extracts outcome-linked heuristics from
recorded trajectories; this selects the top-k for a given task; slice C (#1361)
injects them into the system prompt.

The judge is a SEAM, not a hard dependency
------------------------------------------
ERL specifies an LLM judge that scores each stored heuristic against the new
task. That call belongs to whoever has a model handy, so it enters here as an
optional ``judge`` callable. Without one, ranking falls back to the evidence
slice A already measured — ``outcome_score`` weighted by how much evidence backs
it — which is deterministic, needs no network, and is what the tests exercise.

That ordering is deliberate. A judge that is unavailable, slow, or wrong must
degrade to *ranked by measured outcome*, never to *unranked* or *empty*: an
injection path that silently returns nothing is indistinguishable from one that
had nothing to say.

Task-type match dominates the fallback because a heuristic about coding is
near-useless on a research task no matter how strong its evidence — relevance
gates usefulness, rather than averaging with it.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

SCHEMA_VERSION = "1"

DEFAULT_TOP_K = 5

#: Weight of a task-type match relative to raw evidence. High on purpose: a
#: coding heuristic is near-useless on a research task however strong it is.
_TYPE_MATCH_BONUS = 1.0

#: Evidence beyond this many trajectories stops adding confidence — the
#: difference between 8 and 80 supporting runs is not worth ranking on.
_FREQUENCY_CAP = 8

#: A judge callable takes (task_context, heuristic_dict) and returns 0..1.
Judge = Callable[[str, Dict[str, Any]], float]


@dataclass
class RankedHeuristic:
    """A heuristic with the score that selected it, and where that came from."""

    heuristic: Dict[str, Any]
    score: float
    ranked_by: str  # "judge" | "evidence"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "heuristic": dict(self.heuristic),
            "score": self.score,
            "ranked_by": self.ranked_by,
        }

    @property
    def text(self) -> str:
        return str(self.heuristic.get("text", ""))


def _default_evolution_dir() -> Path:
    env = os.environ.get("EVOLUTION_PROFILE_DIR", "").strip()
    if env:
        return Path(env)
    hh = os.environ.get("HERMES_HOME", "").strip()
    return Path(hh) / "evolution" if hh else Path.home() / ".hermes" / "evolution"


def load_heuristics(evolution_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Load the most recent heuristics file written by slice A.

    Most recent by FILENAME, which is the extraction date — the store is
    append-per-day and the newest extraction reflects the newest trajectories.
    Returns [] rather than raising when nothing has been extracted yet.
    """
    base = (evolution_dir or _default_evolution_dir()) / "heuristics"
    if not base.is_dir():
        return []
    files = sorted(base.glob("*.json"))
    if not files:
        return []
    try:
        data = json.loads(files[-1].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    heuristics = data.get("heuristics") if isinstance(data, dict) else None
    if not isinstance(heuristics, list):
        return []
    return [h for h in heuristics if isinstance(h, dict) and h.get("text")]


def _evidence_score(heuristic: Dict[str, Any], task_type: str) -> float:
    """Rank by what slice A measured, gated on relevance to this task type."""
    try:
        outcome = float(heuristic.get("outcome_score", 0.0) or 0.0)
    except (TypeError, ValueError):
        outcome = 0.0
    try:
        frequency = int(heuristic.get("frequency", 0) or 0)
    except (TypeError, ValueError):
        frequency = 0

    confidence = min(frequency, _FREQUENCY_CAP) / _FREQUENCY_CAP
    score = outcome * confidence
    if task_type and str(heuristic.get("task_type", "")) == task_type:
        score += _TYPE_MATCH_BONUS
    return round(score, 4)


def _dedupe(heuristics: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Drop repeats of the same claim, keeping the first seen.

    Identity is (task_type, pattern) rather than text: the same transition
    extracted on two days produces different counts and therefore different
    prose, but injecting both would spend prompt budget saying one thing twice.
    """
    seen = set()
    out: List[Dict[str, Any]] = []
    for h in heuristics:
        key = (
            str(h.get("task_type", "")),
            tuple(h.get("pattern", []) or []),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(h)
    return out


def retrieve(
    task_context: str,
    heuristics: Sequence[Dict[str, Any]],
    *,
    top_k: int = DEFAULT_TOP_K,
    task_type: str = "",
    judge: Optional[Judge] = None,
) -> List[RankedHeuristic]:
    """Return the top-k heuristics for ``task_context``, best first.

    With a ``judge``, each heuristic is scored by it. Without one — or if it
    raises — ranking falls back to the measured evidence. A judge that fails
    must degrade to ranked-by-evidence, never to empty: an injection path that
    silently returns nothing looks exactly like one with nothing to say.
    """
    candidates = _dedupe([h for h in heuristics if isinstance(h, dict)])
    if not candidates or top_k <= 0:
        return []

    ranked: List[RankedHeuristic] = []
    for h in candidates:
        score: Optional[float] = None
        source = "evidence"
        if judge is not None:
            try:
                raw = judge(task_context, h)
                score = max(0.0, min(1.0, float(raw)))
                source = "judge"
            except Exception:
                score = None
                source = "evidence"
        if score is None:
            score = _evidence_score(h, task_type)
        ranked.append(RankedHeuristic(heuristic=h, score=round(score, 4), ranked_by=source))

    # Highest score first; ties broken by evidence so the order is total and
    # stable rather than dependent on dict iteration.
    ranked.sort(
        key=lambda r: (
            -r.score,
            -float(r.heuristic.get("outcome_score", 0.0) or 0.0),
            -int(r.heuristic.get("frequency", 0) or 0),
            str(r.heuristic.get("text", "")),
        )
    )
    return ranked[:top_k]


def format_for_injection(ranked: Sequence[RankedHeuristic]) -> str:
    """Render the selected heuristics as the block slice C injects.

    Empty string when there is nothing to say, so the caller can concatenate
    unconditionally without emitting an empty header.
    """
    if not ranked:
        return ""
    lines = [
        "# Learned from past runs",
        "Patterns measured across this agent's own completed tasks:",
    ]
    lines.extend(f"- {r.text}" for r in ranked if r.text)
    return "\n".join(lines)


def _usage() -> str:
    return (
        "usage: evolution_heuristic_retrieve.py <task context> [--top-k N]\n"
        "                                       [--task-type T] [--evolution-dir DIR]\n"
        "  Ranks the stored heuristics (#1359) for a task. No LLM: ranks by\n"
        "  measured evidence. Exit 0 ok, 1 nothing stored, 2 bad arguments."
    )


def main(argv: Optional[List[str]] = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if "--help" in args or "-h" in args:
        print(_usage())
        return 0

    top_k = DEFAULT_TOP_K
    task_type = ""
    evolution_dir: Optional[Path] = None

    for flag in ("--top-k", "--task-type", "--evolution-dir"):
        if flag in args:
            i = args.index(flag)
            if i + 1 >= len(args):
                print(_usage(), file=sys.stderr)
                return 2
            value = args[i + 1]
            if flag == "--top-k":
                try:
                    top_k = int(value)
                except ValueError:
                    print("error: --top-k expects a number", file=sys.stderr)
                    return 2
            elif flag == "--task-type":
                task_type = value
            else:
                evolution_dir = Path(value)
            del args[i : i + 2]

    context = " ".join(a for a in args if not a.startswith("--")).strip()
    stored = load_heuristics(evolution_dir)
    if not stored:
        print("[heuristics] none stored — run evolution_heuristic_extract first",
              file=sys.stderr)
        return 1

    ranked = retrieve(context, stored, top_k=top_k, task_type=task_type)
    print(json.dumps(
        {
            "schema_version": SCHEMA_VERSION,
            "task_type": task_type,
            "considered": len(stored),
            "selected": [r.to_dict() for r in ranked],
        },
        indent=2,
        sort_keys=True,
    ))
    print(format_for_injection(ranked), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
