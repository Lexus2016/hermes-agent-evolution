#!/usr/bin/env python3
"""Agent runner for the eval harness (issue #1515, Step 2).

Provides ``EvalTrajectory`` — a dataclass capturing the full execution record
of running the agent against an ``EvalTask`` — and ``run_eval_task()``, the
core runner function that executes a task and records the trajectory.

This is the execution layer of the eval harness foundation. It does NOT
define tasks (Step 1 / #1514) or score results (Step 4).

Dependencies: ``scripts/eval_tasks.py`` (Step 1 / #1514).
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
    """Full execution record of running the agent against one EvalTask.

    Fields per the issue spec:
    - task_id: which EvalTask was run
    - tool_calls: list of {tool, args, result, timestamp} dicts
    - model_turns: number of model round-trips
    - final_answer: the agent's final text output
    - duration_seconds: wall-clock time
    - error: exception if the agent crashed, else None
    """

    task_id: str
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    model_turns: int = 0
    final_answer: str = ""
    duration_seconds: float = 0.0
    error: Optional[str] = None
    # Metadata for reproducibility
    agent_config: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a plain dict suitable for JSON output."""
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
        """Serialize to a single JSONL line (newline-terminated JSON)."""
        return json.dumps(self.to_dict(), ensure_ascii=False) + "\n"


def _make_recorder() -> Tuple[list, Callable]:
    """Build a callback and its backing list for capturing tool calls.

    The callback signature matches the agent's tool-call hook:
    ``callback(tool_name: str, args: dict, result: str)``.
    """
    calls: list = []

    def _record(tool_name: str, args: Any = None, result: Any = None) -> None:
        calls.append(
            {
                "tool": tool_name,
                "args": args if isinstance(args, dict) else {"_raw": str(args)},
                "result": str(result)[:2000],  # truncate large results
                "timestamp": time.time(),
            }
        )

    return calls, _record


def run_eval_task(
    task: Any,
    agent_config: Optional[Dict[str, Any]] = None,
) -> EvalTrajectory:
    """Execute the agent against an ``EvalTask`` and capture the trajectory.

    Parameters
    ----------
    task
        An ``EvalTask`` instance (from ``scripts/eval_tasks.py``).
    agent_config
        Pinned base configuration for reproducibility. Keys:
        - model: model identifier (default "test-pinned")
        - tools: list of enabled tool names (default: ["read_file", "search_files"])
        - system_prompt: system prompt text (default: minimal)
        - max_turns: maximum model round-trips (default: 5)

    Returns
    -------
    EvalTrajectory
        The full execution record.
    """
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
) -> tuple[str, int]:
    """Minimal agent execution loop for eval purposes.

    This is a SIMPLIFIED loop that:
    1. Processes the prompt
    2. Optionally calls tools (in test mode, tools are stubbed)
    3. Returns a final answer

    In production, this would drive the real agent session. For eval
    reproducibility, the loop is deterministic given the same config.

    Returns (final_answer, model_turns).
    """
    max_turns = config.get("max_turns", 5)
    tools = config.get("tools", [])
    turns = 0

    answer_parts: List[str] = []

    # Simulate a minimal reasoning + tool-call cycle.
    # In the real harness this would invoke the model + tool dispatcher.
    for turn_idx in range(max_turns):
        turns += 1
        if turn_idx == 0 and tools:
            # First turn: simulate a tool call to gather information
            record_call(
                tools[0],
                {"prompt": prompt[:200]},
                f"[stub result for {tools[0]}]",
            )
            continue
        # Final turn: produce answer
        answer_parts.append(f"Processed: {prompt[:100]}")
        break

    return "\n".join(answer_parts), turns
