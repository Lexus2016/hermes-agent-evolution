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

Opt-in, and narrower than ``save_trajectories``
-----------------------------------------------
``save_trajectories`` writes the full ShareGPT conversation, user prose
included. This captures **call metadata only**: tool name, redacted arguments
(via the logger's existing ``redact_args``), a status, and a short result
summary. No user message, no assistant prose, no file contents.

It is still **off by default**, behind ``HERMES_EVOLUTION_CAPTURE``. Redaction
catches credential-shaped *keys*, not sensitive *values* in ordinary fields —
a path with a username, an SQL string, an internal hostname all survive it — so
writing on every turn of every interactive session unasked would be the wrong
default even for metadata. The evolution cron stages enable it for their own
runs, which is the behaviour the pipeline actually needs to measure.

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
import hmac
import json
import os
import secrets
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# ``evolution_trajectory_logger`` lives in scripts/, which is not on the
# path for a runtime import from agent/. Adding it here keeps the
# storage format owned by one module instead of duplicating it.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from evolution_trajectory_logger import TrajectoryLog  # noqa: E402

__all__ = [
    "capture_enabled",
    "task_key",
    "extract_tool_calls",
    "build_trajectory_log",
    "capture_turn",
]

#: Result summaries are already truncated by the logger; this bounds the
#: pre-truncation payload so a huge tool result is not held in memory twice.
_MAX_RESULT_CHARS = 2000

#: Opt-in. Default OFF.
#
# Even redacted, tool arguments carry paths containing usernames, SQL, code
# snippets, internal hostnames — ``redact_args`` catches credential-shaped
# KEYS, not sensitive VALUES in ordinary fields. Writing that to disk on every
# turn of every interactive session, unasked and unbounded, is the wrong
# default.
#
# The evolution cron stages set this for their own runs, which is where the
# pipeline's own behaviour is what needs measuring.
_CAPTURE_ENV = "HERMES_EVOLUTION_CAPTURE"

#: Local salt for the pairing key. Without it, a bare hash of a short,
#: templated prompt ("run the tests", "fix issue #102") is recoverable from a
#: rainbow table by anyone who can read the store. HMAC under a host-local
#: secret keeps the key useful for equality while making it useless off-host.
_SALT_FILENAME = ".trajectory_key_salt"


def capture_enabled() -> bool:
    """True when trajectory capture is switched on for this process."""
    return os.environ.get(_CAPTURE_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def _salt(trajectory_dir: Optional[Path] = None) -> bytes:
    """Read (or mint) the host-local salt used for the pairing key.

    Kept beside the trajectories it keys, so a store copied without its salt
    cannot have its task keys correlated against a fresh one. Falls back to a
    process-local random salt when the file cannot be written — pairing then
    only holds within the process, which is strictly better than emitting a
    globally reversible hash.
    """
    if trajectory_dir is None:
        from evolution_trajectory_logger import _default_trajectory_dir

        trajectory_dir = _default_trajectory_dir()
    path = Path(trajectory_dir) / _SALT_FILENAME
    try:
        if path.exists():
            raw = path.read_bytes().strip()
            if raw:
                return raw
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = secrets.token_hex(32).encode("ascii")
        path.write_bytes(raw)
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return raw
    except OSError:
        return secrets.token_hex(32).encode("ascii")


def task_key(task_descriptor: str, trajectory_dir: Optional[Path] = None) -> str:
    """Stable, opaque key for a task, used to pair runs of the same task.

    Salted HMAC rather than a bare hash: #1436 only needs equality to group a
    failed and a successful run of the same task, and the descriptor is user
    prose. A plain ``sha256(prompt)[:16]`` of a short templated instruction is
    recoverable by dictionary attack; an HMAC under a host-local secret is not.
    """
    if not task_descriptor:
        return ""
    return hmac.new(
        _salt(trajectory_dir),
        task_descriptor.encode("utf-8", "replace"),
        hashlib.sha256,
    ).hexdigest()[:16]


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


def extract_tool_calls(
    messages: List[Dict[str, Any]],
    timings: Optional[Dict[str, int]] = None,
) -> List[Dict[str, Any]]:
    """Pull ``(tool, args, result)`` triples out of a finished turn's messages.

    Walks assistant turns for ``tool_calls`` and matches each to its ``tool``
    result by ``tool_call_id``. A call with no matching result (the turn ended
    mid-flight) is kept with a ``pending`` status rather than dropped — a call
    that never returned is itself signal for #1268's error-recovery dimension.

    ``timings`` maps ``tool_call_id`` to milliseconds, collected during the turn
    by ``agent_runtime_helpers`` — the only place per-call duration exists, since
    by the time this runs the calls are long finished (#1442).
    """
    timings = timings or {}
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
                    "duration_ms": timings.get(cid) if cid else None,
                }
            )
    return calls


def build_trajectory_log(
    messages: List[Dict[str, Any]],
    *,
    session_id: str = "",
    task_descriptor: str = "",
    completed: bool = True,
    trajectory_dir: Optional[Path] = None,
    timings: Optional[Dict[str, int]] = None,
) -> Optional[TrajectoryLog]:
    """Build a :class:`TrajectoryLog` from a finished turn, or None if empty.

    Returns None when the turn made no tool calls: a trajectory with no actions
    tells every consumer nothing and would only dilute the store.
    """
    calls = extract_tool_calls(messages, timings)
    if not calls:
        return None

    log = TrajectoryLog(
        session_id=session_id or "",
        completed=bool(completed),
        task_key=task_key(task_descriptor, trajectory_dir),
    )
    for call in calls:
        log.add_tool_call(
            call["tool"],
            call["args"],
            result=call["result"],
            status=call["status"],
            duration_ms=call.get("duration_ms"),
        )
    return log


def capture_turn(
    messages: List[Dict[str, Any]],
    *,
    session_id: str = "",
    task_descriptor: str = "",
    completed: bool = True,
    trajectory_dir: Optional[Path] = None,
    timings: Optional[Dict[str, int]] = None,
) -> Optional[Path]:
    """Build and persist a turn's trajectory. Returns the path, or None.

    Never raises. This runs in ``finalize_turn`` alongside teardown that is
    already individually guarded (#8049) — instrumentation must not be able to
    discard a completed turn's response.
    """
    if not capture_enabled():
        return None
    try:
        log = build_trajectory_log(
            messages,
            session_id=session_id,
            task_descriptor=task_descriptor,
            completed=completed,
            trajectory_dir=trajectory_dir,
            timings=timings,
        )
        if log is None:
            return None
        # append, not save: save() overwrites, so every turn after the first
        # would destroy the previous one and a multi-turn session would keep
        # only its last turn.
        return log.append(trajectory_dir)
    except Exception:
        return None
