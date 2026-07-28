#!/usr/bin/env python3
"""Cross-task heuristic extraction from recorded trajectories (issue #1359).

Child A of #1303 (ERL — Experiential Reflective Learning, arXiv:2603.24639).
ERL distils each completed ``{task, trajectory, outcome}`` into a reusable
heuristic; slice B (#1360) reranks them for a new task and slice C (#1361)
injects the top-k into the system prompt.

This slice is the extractor, and it is deliberately DETERMINISTIC — no LLM.
The heuristic text is generated from measured contrast, not written by a model:
a tool sequence that appears in successful runs of a task type and is absent
from failed ones is evidence, and evidence is what slice B should rerank. A
model-authored paragraph over the same data would add fluency and remove
falsifiability.

Input is the #1363 capture — ``<evolution_dir>/trajectories/*.jsonl``, one JSON
object per turn carrying redacted call metadata and a task-level ``completed``
outcome. Output is ``<evolution_dir>/heuristics/<date>.json``.

What makes a heuristic here
---------------------------
A tool bigram (an ordered pair of consecutive calls) scored by the CONTRAST
between how often it appears in successful versus failed trajectories of the
same task type. A pair common to both tells you nothing; a pair that shows up
only when things go right is the signal ERL is after.

Bigrams rather than whole sequences because whole sequences almost never repeat
across tasks — the recurrence ERL depends on lives at the level of "after
reading a file, patch it" rather than "this exact 14-step run".
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SCHEMA_VERSION = "1"

#: A pattern must appear in at least this many distinct trajectories before it
#: is called recurring. Two is the minimum that can distinguish a habit from a
#: one-off.
DEFAULT_MIN_FREQUENCY = 2

#: Outcome score below which a heuristic is not worth surfacing: at 0.0 the
#: pattern is equally common in successes and failures, so it carries no
#: information about the outcome.
DEFAULT_MIN_SCORE = 0.25


@dataclass
class Heuristic:
    """One extracted, falsifiable claim about what works for a task type."""

    task_type: str
    pattern: List[str] = field(default_factory=list)
    text: str = ""
    frequency: int = 0
    success_count: int = 0
    failure_count: int = 0
    outcome_score: float = 0.0
    source_trajectories: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_type": self.task_type,
            "pattern": list(self.pattern),
            "text": self.text,
            "frequency": self.frequency,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "outcome_score": self.outcome_score,
            "source_trajectories": list(self.source_trajectories),
        }


def _default_evolution_dir() -> Path:
    env = os.environ.get("EVOLUTION_PROFILE_DIR", "").strip()
    if env:
        return Path(env)
    hh = os.environ.get("HERMES_HOME", "").strip()
    return Path(hh) / "evolution" if hh else Path.home() / ".hermes" / "evolution"


def load_trajectories(trajectory_dir: Path) -> List[Dict[str, Any]]:
    """Read captured turns that carry a RECORDED outcome.

    ``completed`` is tri-state in the capture: True and False are recorded,
    absent means "not recorded" (pre-#1363 cron trajectories). A heuristic is a
    claim about what leads to success, so a trajectory whose outcome nobody
    wrote down cannot contribute to either side of the contrast and is skipped.
    """
    out: List[Dict[str, Any]] = []
    if not trajectory_dir.is_dir():
        return out
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
            if not isinstance(rec.get("completed"), bool):
                continue
            rec["_id"] = f"{path.stem}#{idx}"
            out.append(rec)
    return out


def _tools(record: Dict[str, Any]) -> List[str]:
    return [
        str(e.get("tool"))
        for e in record.get("entries", [])
        if isinstance(e, dict) and e.get("tool")
    ]


def _classify(tools: List[str]) -> str:
    """Group trajectories by what they did, reusing the store's taxonomy.

    Imported lazily so this module stays usable if that script moves; falling
    back to a single bucket keeps extraction working rather than failing, at
    the cost of coarser grouping.
    """
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from evolution_trajectory_store import classify_by_tools

        return classify_by_tools(tools)
    except Exception:
        return "general"


def _bigrams(tools: List[str]) -> List[Tuple[str, str]]:
    return [(tools[i], tools[i + 1]) for i in range(len(tools) - 1)]


def extract_heuristics(
    records: List[Dict[str, Any]],
    *,
    min_frequency: int = DEFAULT_MIN_FREQUENCY,
    min_score: float = DEFAULT_MIN_SCORE,
) -> List[Heuristic]:
    """Rank tool-transition patterns by how much they predict success.

    ``outcome_score`` is ``(successes - failures) / total`` for the pattern
    within its task type: 1.0 means it only ever appeared in successful runs,
    0.0 means it is equally common in both and therefore says nothing, and a
    negative score means it is associated with failure. Only positive scores
    are returned — a heuristic is advice to follow, and slice C injects these
    into a prompt.
    """
    by_type: Dict[str, Dict[Tuple[str, str], Dict[str, Any]]] = {}

    for rec in records:
        tools = _tools(rec)
        if len(tools) < 2:
            continue
        task_type = _classify(tools)
        completed = bool(rec.get("completed"))
        bucket = by_type.setdefault(task_type, {})
        # Count each pattern ONCE per trajectory: a loop repeating the same
        # transition ten times is one habit, not ten pieces of evidence.
        for pair in set(_bigrams(tools)):
            stat = bucket.setdefault(
                pair, {"success": 0, "failure": 0, "sources": []}
            )
            stat["success" if completed else "failure"] += 1
            stat["sources"].append(str(rec.get("_id", "")))

    heuristics: List[Heuristic] = []
    for task_type, patterns in by_type.items():
        for pair, stat in patterns.items():
            total = stat["success"] + stat["failure"]
            if total < min_frequency:
                continue
            score = round((stat["success"] - stat["failure"]) / total, 4)
            if score < min_score:
                continue
            heuristics.append(
                Heuristic(
                    task_type=task_type,
                    pattern=list(pair),
                    text=(
                        f"On {task_type} tasks, following `{pair[0]}` with "
                        f"`{pair[1]}` appeared in {stat['success']} successful "
                        f"and {stat['failure']} failed run(s)."
                    ),
                    frequency=total,
                    success_count=stat["success"],
                    failure_count=stat["failure"],
                    outcome_score=score,
                    source_trajectories=sorted(set(s for s in stat["sources"] if s)),
                )
            )

    # Strongest signal first, then most evidence, then stable by name.
    heuristics.sort(
        key=lambda h: (-h.outcome_score, -h.frequency, h.task_type, h.pattern)
    )
    return heuristics


def write_heuristics(
    heuristics: List[Heuristic],
    evolution_dir: Optional[Path] = None,
    date: Optional[str] = None,
) -> Path:
    """Persist to ``<evolution_dir>/heuristics/<date>.json``."""
    base = evolution_dir or _default_evolution_dir()
    day = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out_dir = base / "heuristics"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{day}.json"
    payload = {
        "schema_version": SCHEMA_VERSION,
        "date": day,
        "count": len(heuristics),
        "heuristics": [h.to_dict() for h in heuristics],
    }
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return path


def format_summary(heuristics: List[Heuristic]) -> str:
    if not heuristics:
        return "[heuristics] none extracted (no recurring outcome-linked patterns)"
    by_type = Counter(h.task_type for h in heuristics)
    spread = " ".join(f"{k}={v}" for k, v in sorted(by_type.items()))
    top = heuristics[0]
    return (
        f"[heuristics] {len(heuristics)} extracted ({spread}); "
        f"strongest: {'->'.join(top.pattern)} on {top.task_type} "
        f"(score {top.outcome_score:.2f}, n={top.frequency})"
    )


def main(argv: Optional[List[str]] = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if "--help" in args or "-h" in args:
        print(
            "usage: evolution_heuristic_extract.py [--evolution-dir DIR]\n"
            "                                      [--min-frequency N] [--min-score F]\n"
            "  Extracts outcome-linked tool-transition heuristics from the\n"
            "  captured trajectories (#1363). Writes heuristics/<date>.json.\n"
            "  Exit 0 ok, 1 nothing extracted, 2 bad arguments."
        )
        return 0

    evolution_dir = _default_evolution_dir()
    min_frequency = DEFAULT_MIN_FREQUENCY
    min_score = DEFAULT_MIN_SCORE

    for flag, caster in (
        ("--evolution-dir", None),
        ("--min-frequency", int),
        ("--min-score", float),
    ):
        if flag in args:
            i = args.index(flag)
            if i + 1 >= len(args):
                print(f"error: {flag} needs a value", file=sys.stderr)
                return 2
            raw = args[i + 1]
            try:
                if flag == "--evolution-dir":
                    evolution_dir = Path(raw)
                elif flag == "--min-frequency":
                    min_frequency = caster(raw)
                else:
                    min_score = caster(raw)
            except ValueError:
                print(f"error: {flag} expects a number", file=sys.stderr)
                return 2

    records = load_trajectories(evolution_dir / "trajectories")
    heuristics = extract_heuristics(
        records, min_frequency=min_frequency, min_score=min_score
    )
    path = write_heuristics(heuristics, evolution_dir)

    print(
        json.dumps(
            {
                "trajectories_read": len(records),
                "heuristics": [h.to_dict() for h in heuristics],
                "written_to": str(path),
            },
            indent=2,
            sort_keys=True,
        )
    )
    print(format_summary(heuristics), file=sys.stderr)
    return 0 if heuristics else 1


if __name__ == "__main__":
    sys.exit(main())
