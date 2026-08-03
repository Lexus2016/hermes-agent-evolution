#!/usr/bin/env python3
"""Agent runner for the eval harness (issue #1515, Step 2).

Provides ``EvalTrajectory`` and ``run_eval_task()`` — the execution layer
of the eval harness foundation. Does NOT define tasks or score results.
"""

from __future__ import annotations

import json
import logging
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


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


def _make_recorder() -> Tuple[list, Callable]:
    """Build a tool-call capture callback and its backing list."""
    calls: list = []

    def _record(tool_name: str, args: Any = None, result: Any = None) -> None:
        calls.append(
            {
                "tool": tool_name,
                "args": args if isinstance(args, dict) else {"_raw": str(args)},
                "result": str(result)[:2000],
                "timestamp": time.time(),
            }
        )

    return calls, _record


def run_eval_task(
    task: Any,
    agent_config: Optional[Dict[str, Any]] = None,
) -> EvalTrajectory:
    """Execute the agent against an ``EvalTask`` and capture the trajectory."""
    cfg = agent_config or {}
    pinned_config: Dict[str, Any] = {
        "model": cfg.get("model", "test-pinned"),
        "tools": cfg.get("tools", ["read_file", "search_files"]),
        "system_prompt": cfg.get("system_prompt", "You are a test agent."),
        "max_turns": cfg.get("max_turns", 5),
    }

    task_id = getattr(task, "id", str(task))
    prompt = getattr(task, "prompt", str(task))
    traj = EvalTrajectory(task_id=task_id, agent_config=pinned_config)

    tool_calls, recorder = _make_recorder()
    start = time.time()

    try:
        answer, turns = _execute_agent_loop(prompt, pinned_config, recorder)
        traj.final_answer = answer
        traj.model_turns = turns
    except Exception as exc:
        traj.error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"

    traj.duration_seconds = time.time() - start
    traj.tool_calls = tool_calls
    return traj


def _execute_agent_loop(
    prompt: str,
    config: Dict[str, Any],
    record_call: Callable,
) -> Tuple[str, int]:
    """Minimal deterministic agent loop for eval reproducibility.

    In production this would drive the real agent session.
    """
    max_turns = config.get("max_turns", 5)
    tools = config.get("tools", [])
    turns = 0
    answer_parts: List[str] = []

    for turn_idx in range(max_turns):
        turns += 1
        if turn_idx == 0 and tools:
            record_call(
                tools[0],
                {"prompt": prompt[:200]},
                f"[stub result for {tools[0]}]",
            )
            continue
        answer_parts.append(f"Processed: {prompt[:100]}")
        break

    return "\n".join(answer_parts), turns
