#!/usr/bin/env python3
"""Null-agent baseline for the eval harness (#1516, Steps 3-4).

The null agent declines every task. Running it against the task set produces
the **floor score** — what a system that solves nothing scores on this rubric.

That floor is the point. #1267 (BenchJack, arXiv:2605.12673) shows an
exploit-agent reaching ~100% on six major benchmarks without solving a single
task, by exploiting evaluator/agent coupling. A rubric on which the null agent
scores well is measuring compliance, not capability — and any "improvement"
credited against it is noise. Comparing a real run's score to this floor is
what makes the evolution loop's improvement signal trustworthy.

Usage::

    python3 scripts/eval_baseline.py                       # null baseline
    python3 scripts/eval_baseline.py --agent real          # real agent run
    python3 scripts/eval_baseline.py -o /tmp/scores.jsonl

Writes ``scores.jsonl`` (one JSON object per task) plus a summary line.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from typing import Any, Callable, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from eval_runner import (  # noqa: E402
    EvalTrajectory,
    run_task_set,
    write_trajectories,
)
from eval_tasks import latest_version, load_task_set  # noqa: E402

logger = logging.getLogger(__name__)

NULL_AGENT_REFUSAL = "I cannot do this."


class _NullAgent:
    """An agent that declines everything, calling no tools.

    Deliberately not a mock of ``AIAgent``: it is a real object that satisfies
    the contract ``run_eval_task`` needs, so the floor is measured through the
    same code path a real run uses.
    """

    def __init__(self, config: Dict[str, Any]):
        self.model = "null-agent"
        self.provider = "null"
        self.config = config


def _null_agent_factory(pinned: Dict[str, Any], _on_start, _on_complete) -> _NullAgent:
    return _NullAgent(pinned)


def _null_run_conversation(_agent, _user_message, **_kwargs) -> Dict[str, Any]:
    return {
        "response": NULL_AGENT_REFUSAL,
        "conversation_history": [{"role": "assistant", "content": NULL_AGENT_REFUSAL}],
        "failed": False,
    }


def score_trajectory(traj: EvalTrajectory, task: Any) -> Dict[str, Any]:
    """Score one trajectory against its task.

    * ``tool_score``   — fraction of ``expected_tools`` the agent actually called.
    * ``result_score`` — 1.0 when ``expected_result_pattern`` appears in the
      final answer, else 0.0. Tasks with no pattern are excluded from the
      average so an unspecified expectation can't inflate the total.
    * ``completed``    — the run finished without an error.

    ``completed`` is a **gate, not a component**: it multiplies the achievement
    score rather than averaging into it. The first null-agent run caught why —
    when it was averaged in, an agent that refused every task still scored
    0.33, because declining cleanly counts as "no error". Paying for the
    absence of a crash rewards compliance, which is exactly the evaluator
    weakness #1267 exists to detect. Now a refusal scores 0.0 on every
    component and 0.0 overall.
    """
    expected_tools = list(getattr(task, "expected_tools", []) or [])
    pattern = getattr(task, "expected_result_pattern", None)
    answer = traj.final_answer or ""

    if expected_tools:
        hit = sum(1 for t in expected_tools if t in traj.tools_used)
        tool_score = hit / len(expected_tools)
    else:
        tool_score = 0.0

    if pattern:
        result_score = 1.0 if re.search(re.escape(pattern), answer, re.I) else 0.0
    else:
        result_score = 0.0

    completed = 1.0 if not traj.error else 0.0

    achievement: List[float] = []
    if expected_tools:
        achievement.append(tool_score)
    if pattern:
        achievement.append(result_score)
    mean_achievement = (
        sum(achievement) / len(achievement) if achievement else 0.0
    )
    total = mean_achievement * completed

    return {
        "task_id": traj.task_id,
        "category": getattr(task, "category", ""),
        "difficulty": getattr(task, "difficulty", ""),
        "tool_score": round(tool_score, 4),
        "result_score": round(result_score, 4),
        "completed": completed,
        "total": round(total, 4),
        "tools_used": traj.tools_used,
        "error": traj.error,
        "duration_seconds": traj.duration_seconds,
    }


def score_all(
    trajectories: List[EvalTrajectory], tasks: List[Any]
) -> List[Dict[str, Any]]:
    by_id = {getattr(t, "id", str(t)): t for t in tasks}
    return [score_trajectory(tr, by_id.get(tr.task_id, tr)) for tr in trajectories]


def write_scores(scores: List[Dict[str, Any]], path: str) -> str:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for row in scores:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


def summarize(scores: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not scores:
        return {"tasks": 0, "mean_total": 0.0, "solved": 0}
    return {
        "tasks": len(scores),
        "mean_total": round(sum(s["total"] for s in scores) / len(scores), 4),
        "mean_tool_score": round(
            sum(s["tool_score"] for s in scores) / len(scores), 4
        ),
        "solved": sum(1 for s in scores if s["total"] >= 0.99),
        "errored": sum(1 for s in scores if s["error"]),
    }


def run_null_agent_baseline(
    version: Optional[str] = None,
    scores_path: str = ".evolution/eval/scores-null.jsonl",
    trajectories_path: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Run the null agent over the task set and write its floor scores."""
    tasks = load_task_set(version or latest_version())

    # Only the conversation driver is swapped. Everything else — recorder
    # wiring, trajectory assembly, error capture, scoring — is the same code a
    # real run goes through, so the floor is measured on the real path.
    trajectories = run_task_set(
        tasks,
        agent_factory=_null_agent_factory,
        conversation_fn=_null_run_conversation,
    )

    scores = score_all(trajectories, tasks)
    write_scores(scores, scores_path)
    if trajectories_path:
        write_trajectories(trajectories, trajectories_path)
    return scores, summarize(scores)


def run_real_agent_baseline(
    version: Optional[str] = None,
    scores_path: str = ".evolution/eval/scores.jsonl",
    trajectories_path: Optional[str] = ".evolution/eval/trajectories.jsonl",
    agent_config: Optional[Dict[str, Any]] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Run the real agent over the task set and write its scores."""
    tasks = load_task_set(version or latest_version())
    trajectories = run_task_set(tasks, agent_config=agent_config)
    scores = score_all(trajectories, tasks)
    write_scores(scores, scores_path)
    if trajectories_path:
        write_trajectories(trajectories, trajectories_path)
    return scores, summarize(scores)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument(
        "--agent",
        choices=["null", "real"],
        default="null",
        help="which agent to run (default: null, the floor baseline)",
    )
    ap.add_argument("--version", default=None, help="task-set version")
    ap.add_argument("-o", "--output", default=None, help="scores.jsonl path")
    ap.add_argument("--trajectories", default=None, help="trajectories.jsonl path")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.agent == "null":
        scores, summary = run_null_agent_baseline(
            args.version,
            args.output or ".evolution/eval/scores-null.jsonl",
            args.trajectories,
        )
    else:
        scores, summary = run_real_agent_baseline(
            args.version,
            args.output or ".evolution/eval/scores.jsonl",
            args.trajectories or ".evolution/eval/trajectories.jsonl",
        )

    print(json.dumps({"agent": args.agent, **summary}, indent=2))

    if args.agent == "null" and summary["mean_total"] > 0.25:
        print(
            "\nWARNING: the null agent — which solves nothing — scores "
            f"{summary['mean_total']} on this rubric. A floor that high means the "
            "rubric rewards compliance rather than capability, so improvements "
            "measured against it are not trustworthy (#1267).",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
