#!/usr/bin/env python3
"""Agent runner for the eval harness (#1515, Step 2).

Execution layer only: drives a real ``AIAgent`` against an ``EvalTask`` from
``eval_tasks`` and records what happened. It does not define tasks (Step 1,
``eval_tasks.py``) and does not score results (Step 3-4, ``eval_baseline.py``).

The agent is constructed from a **pinned** config so a trajectory captured
today is comparable to one captured next month: same model, same toolset, same
iteration cap. Tool calls are captured through the agent's own
``tool_start_callback`` / ``tool_complete_callback`` hooks, so the record
reflects what the agent actually did rather than a reconstruction.

Anything that goes wrong while running a task is captured into
``EvalTrajectory.error`` instead of raising: one task blowing up must not
abort a whole baseline run, and "this task errored" is itself a result worth
scoring.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Defaults for a pinned eval agent. Deliberately small: eval tasks are short,
# and an unbounded iteration cap turns one stuck task into a stalled run.
DEFAULT_MODEL = os.environ.get("HERMES_EVAL_MODEL", "")
DEFAULT_TOOLSETS = ["files"]
DEFAULT_MAX_ITERATIONS = 8


@dataclass
class EvalTrajectory:
    """Full execution record of running the agent against one EvalTask."""

    task_id: str
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    model_turns: int = 0
    final_answer: str = ""
    duration_seconds: float = 0.0
    error: Optional[str] = None
    agent_config: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "tool_calls": list(self.tool_calls),
            "model_turns": self.model_turns,
            "final_answer": self.final_answer,
            "duration_seconds": round(self.duration_seconds, 3),
            "error": self.error,
            "agent_config": dict(self.agent_config),
        }

    def to_jsonl(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False) + "\n"

    @property
    def tools_used(self) -> List[str]:
        """Distinct tool names in call order — the shape scoring compares."""
        seen: List[str] = []
        for call in self.tool_calls:
            name = call.get("tool", "")
            if name and name not in seen:
                seen.append(name)
        return seen


def _make_recorder() -> Tuple[List[Dict[str, Any]], Callable, Callable]:
    """Build the tool-call capture callbacks and their backing list.

    Returns ``(calls, on_start, on_complete)``. ``on_start`` appends the call;
    ``on_complete`` attaches the result to the most recent matching entry, so a
    tool that never returns still leaves a record that it was attempted.
    """
    calls: List[Dict[str, Any]] = []

    def on_start(tool_name: str, args: Any = None, **_kw: Any) -> None:
        calls.append(
            {
                "tool": tool_name,
                "args": args if isinstance(args, dict) else {"_raw": str(args)[:500]},
                "result": None,
                "started_at": time.time(),
            }
        )

    def on_complete(tool_name: str, result: Any = None, **_kw: Any) -> None:
        for call in reversed(calls):
            if call["tool"] == tool_name and call["result"] is None:
                call["result"] = str(result)[:2000]
                return
        # A completion with no matching start (a tool invoked through a path
        # that skips the start hook) still belongs in the record.
        calls.append(
            {
                "tool": tool_name,
                "args": {},
                "result": str(result)[:2000],
                "started_at": time.time(),
            }
        )

    return calls, on_start, on_complete


def build_pinned_config(overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Resolve the pinned agent config for an eval run."""
    cfg = overrides or {}
    return {
        "model": cfg.get("model", DEFAULT_MODEL),
        "toolsets": list(cfg.get("toolsets", DEFAULT_TOOLSETS)),
        "max_iterations": int(cfg.get("max_iterations", DEFAULT_MAX_ITERATIONS)),
        "system_prompt": cfg.get("system_prompt") or "",
    }


def _build_agent(pinned: Dict[str, Any], on_start: Callable, on_complete: Callable):
    """Construct a real AIAgent wired to the trajectory recorder."""
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    from run_agent import AIAgent  # noqa: PLC0415 — heavy import, keep it lazy

    kwargs: Dict[str, Any] = {
        "model": pinned["model"],
        "max_iterations": pinned["max_iterations"],
        "enabled_toolsets": pinned["toolsets"],
        "quiet_mode": True,
        "tool_start_callback": on_start,
        "tool_complete_callback": on_complete,
    }
    if pinned.get("system_prompt"):
        kwargs["ephemeral_system_prompt"] = pinned["system_prompt"]
    return AIAgent(**kwargs)


def run_eval_task(
    task: Any,
    agent_config: Optional[Dict[str, Any]] = None,
    agent_factory: Optional[Callable] = None,
    conversation_fn: Optional[Callable] = None,
) -> EvalTrajectory:
    """Execute the agent against one ``EvalTask`` and capture the trajectory.

    ``agent_factory`` and ``conversation_fn`` exist so a caller can supply a
    faithful double — the null agent in :mod:`eval_baseline` uses them to
    establish the floor score without burning model calls, while still going
    through this exact recorder wiring, trajectory assembly and error capture.
    When omitted, a real ``AIAgent`` is driven by the real conversation loop.
    """
    pinned = build_pinned_config(agent_config)
    task_id = getattr(task, "id", str(task))
    prompt = getattr(task, "prompt", str(task))

    traj = EvalTrajectory(task_id=task_id, agent_config=pinned)
    tool_calls, on_start, on_complete = _make_recorder()
    start = time.time()

    try:
        factory = agent_factory or _build_agent
        agent = factory(pinned, on_start, on_complete)

        drive = conversation_fn
        if drive is None:
            from agent.conversation_loop import run_conversation  # noqa: PLC0415

            drive = run_conversation

        result = drive(
            agent,
            prompt,
            system_message=pinned.get("system_prompt") or None,
            task_id=f"eval-{task_id}",
        )

        if isinstance(result, dict):
            traj.final_answer = str(
                result.get("response") or result.get("content") or ""
            )
            history = result.get("conversation_history") or []
            traj.model_turns = sum(
                1 for m in history if isinstance(m, dict) and m.get("role") == "assistant"
            )
            if result.get("failed"):
                traj.error = str(result.get("error") or "agent reported failure")
        else:
            traj.final_answer = str(result or "")
    except Exception as exc:  # noqa: BLE001 — a failed task is a result, not a crash
        traj.error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
        logger.warning("eval task %s failed: %s", task_id, exc)

    traj.duration_seconds = time.time() - start
    traj.tool_calls = tool_calls
    if not traj.model_turns and traj.final_answer:
        # Providers that don't return a history still completed a turn.
        traj.model_turns = 1
    return traj


def run_task_set(
    tasks: List[Any],
    agent_config: Optional[Dict[str, Any]] = None,
    agent_factory: Optional[Callable] = None,
    conversation_fn: Optional[Callable] = None,
) -> List[EvalTrajectory]:
    """Run every task in order, returning one trajectory each."""
    trajectories: List[EvalTrajectory] = []
    for task in tasks:
        logger.info("running eval task %s", getattr(task, "id", task))
        trajectories.append(
            run_eval_task(task, agent_config, agent_factory, conversation_fn)
        )
    return trajectories


def write_trajectories(trajectories: List[EvalTrajectory], path: str) -> str:
    """Write trajectories as JSONL, returning the path written."""
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for traj in trajectories:
            fh.write(traj.to_jsonl())
    return path
