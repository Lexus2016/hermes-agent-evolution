#!/usr/bin/env python3
"""Capture the agent's real tool calls for the evolution pipeline (issue #1363).

The pipeline had no record of what the agent actually did. ``evolution_funnel``
writes a trajectory containing one entry — ``tool='evolution_funnel'``, the
pipeline logging its own invocation — and ``agent.save_trajectories`` writes real
conversation JSONL but defaults off, lands in the process CWD, and nothing in the
pipeline reads it.

That one missing input has cost six PRs. #1267, #1268 and #1270 each had two
closed by the owner as *dead code* or *incoherent*, with the same verdict every
time: the implementation "reads the funnel's own synthetic trajectory record
rather than real agent tool calls". Each attempt had to invent a substitute for
data that did not exist. Seven issues wait on it today (#1267, #1268, #1270,
#1359, #1360, #1361, #1436, #1442).

What this module adds is the conversion, not new storage: it turns a finished
turn's message list into the ``TrajectoryLog`` that ``evolution_trajectory_logger``
already defines and already knows how to persist under
``~/.hermes/evolution/trajectories/``.

Privacy — why this can be on when ``save_trajectories`` is off
--------------------------------------------------------------
``save_trajectories`` writes the full ShareGPT conversation, user prose
included, which is why it is off by default and why turning it on for evolution
would be the wrong trade. This captures **call metadata only**: tool name,
redacted arguments (via the logger's existing ``redact_args``), a status, and a
short result summary. No user message, no assistant prose, no file contents.

The consumers want different shapes, so the record carries the union
-------------------------------------------------------------------
* #1268 (tool-use rubric) needs per-call tool / args / outcome / order.
* #1359 (ERL heuristics) needs a task-level **outcome** to know whether a
  trajectory is worth distilling from.
* #1436 (EMG graph matching) needs to **pair** a failed and a successful
  trajectory for the same task, so it needs a task descriptor that is stable
  across runs and does not leak the prompt.

Hence ``task_key``: a short hash of the task descriptor. Stable enough to pair
on, opaque enough to store.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# ``evolution_trajectory_logger`` lives in scripts/, which is not on the
# path for a runtime import from agent/. Adding it here keeps the
# storage format owned by one module instead of duplicating it.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from evolution_trajectory_logger import TrajectoryLog  # noqa: E402

__all__ = ["task_key", "extract_tool_calls", "build_trajectory_log", "capture_turn"]

#: Result summaries are already truncated by the logger; this bounds the
#: pre-truncation payload so a huge tool result is not held in memory twice.
_MAX_RESULT_CHARS = 2000


def task_key(task_descriptor: str) -> str:
    """Stable, opaque key for a task, used to pair runs of the same task.

    A hash rather than the text: #1436 needs to group a failed and a successful
    run of the *same* task, which only needs equality, while the descriptor
    itself is user prose that must not land in the pipeline's store.
    """
    if not task_descriptor:
        return ""
    return hashlib.sha256(task_descriptor.encode("utf-8", "replace")).hexdigest()[:16]


def _result_status(content: Any) -> str:
    """Classify a tool result as success/failure using the repo's own predicate.

    Reuses ``introspection_extract._tool_result_failed`` so this agrees with the
    digest instead of inventing a second definition of failure — two
    disagreeing classifiers is how a metric drifts from what it claims.
    """
    try:
        from introspection_extract import _tool_result_failed

        return "failure" if _tool_result_failed(content) else "success"
    except Exception:
        # Fall back to the same fail-open default the digest uses: without an
        # authoritative status, do not guess a failure.
        return "success"


def extract_tool_calls(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Pull ``(tool, args, result)`` triples out of a finished turn's messages.

    Walks assistant turns for ``tool_calls`` and matches each to its ``tool``
    result by ``tool_call_id``. A call with no matching result (the turn ended
    mid-flight) is kept with a ``pending`` status rather than dropped — a call
    that never returned is itself signal for #1268's error-recovery dimension.
    """
    if not isinstance(messages, list):
        return []

    results: Dict[str, Any] = {}
    for msg in messages:
        if isinstance(msg, dict) and msg.get("role") == "tool":
            cid = msg.get("tool_call_id")
            if cid:
                results[cid] = msg.get("content")

    calls: List[Dict[str, Any]] = []
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        for tc in msg.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function")
            if not isinstance(fn, dict):
                continue
            name = fn.get("name")
            if not name:
                continue
            raw_args = fn.get("arguments")
            if isinstance(raw_args, str):
                try:
                    args = json.loads(raw_args)
                except (json.JSONDecodeError, ValueError):
                    args = {}
            elif isinstance(raw_args, dict):
                args = raw_args
            else:
                args = {}
            cid = tc.get("id")
            has_result = cid in results
            content = results.get(cid)
            if isinstance(content, str) and len(content) > _MAX_RESULT_CHARS:
                content = content[:_MAX_RESULT_CHARS]
            calls.append(
                {
                    "tool": str(name),
                    "args": args if isinstance(args, dict) else {},
                    "result": content,
                    "status": _result_status(content) if has_result else "pending",
                }
            )
    return calls


def build_trajectory_log(
    messages: List[Dict[str, Any]],
    *,
    session_id: str = "",
    task_descriptor: str = "",
    completed: bool = True,
) -> Optional[TrajectoryLog]:
    """Build a :class:`TrajectoryLog` from a finished turn, or None if empty.

    Returns None when the turn made no tool calls: a trajectory with no actions
    tells every consumer nothing and would only dilute the store.
    """
    calls = extract_tool_calls(messages)
    if not calls:
        return None

    log = TrajectoryLog(
        session_id=session_id or "",
        completed=bool(completed),
        task_key=task_key(task_descriptor),
    )
    for call in calls:
        log.add_tool_call(
            call["tool"],
            call["args"],
            result=call["result"],
            status=call["status"],
        )
    return log


def capture_turn(
    messages: List[Dict[str, Any]],
    *,
    session_id: str = "",
    task_descriptor: str = "",
    completed: bool = True,
    trajectory_dir: Optional[Path] = None,
) -> Optional[Path]:
    """Build and persist a turn's trajectory. Returns the path, or None.

    Never raises. This runs in ``finalize_turn`` alongside teardown that is
    already individually guarded (#8049) — instrumentation must not be able to
    discard a completed turn's response.
    """
    try:
        log = build_trajectory_log(
            messages,
            session_id=session_id,
            task_descriptor=task_descriptor,
            completed=completed,
        )
        if log is None:
            return None
        return log.save(trajectory_dir)
    except Exception:
        return None
