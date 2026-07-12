"""A2A (Agent2Agent) server: accept JSON-RPC task submissions and map each to a
Hermes tool call.

Slice 3 of #748 (issue #881). Builds on the Slice-1 discovery view
(``hermes_cli/a2a.py`` + ``/.well-known/agent.json``): this module turns an
incoming A2A ``message/send`` / ``message/stream`` into a single dispatch
through the *existing* Hermes tool registry (``tools.registry.registry``). It
adds **no** core tools -- it is a thin JSON-RPC front over tools Hermes already
has.

Security posture (the route wiring in ``web_server.py`` owns transport auth):
  * The route calls ``_require_token`` so unauthenticated callers never reach
    task execution (loopback: session token/bearer; non-loopback: the OAuth
    gate 401/302s first). This module assumes the caller is already authorized.
  * Even for an authorized caller, only tools **advertised on the Agent Card**
    are dispatchable (:func:`_authorize_tool`). This honours the card's
    ``expose``/``exclude`` blocklist, so a tool Hermes deliberately hid is not
    reachable over A2A. Unknown/hidden tools are indistinguishable (no oracle).
  * Requests are size-capped (route) and each tool runs under a wall-clock
    timeout (:data:`TOOL_TIMEOUT_S`) in a worker thread, so a slow/hung tool
    can't block the event loop or hold an SSE stream open forever.

Task state lives here (the "shared task-state type" for this wave):
:class:`A2ATask` + the in-memory bounded :class:`TaskStore`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from threading import Lock
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# JSON-RPC 2.0 error codes (+ the A2A TaskNotFound extension, -32001).
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
TASK_NOT_FOUND = -32001

# Resource caps. MAX_REQUEST_BYTES is enforced by the route on the raw body;
# it is exported here so both sides share one number.
MAX_REQUEST_BYTES = 256 * 1024
TOOL_TIMEOUT_S = 60.0
MAX_TASKS = 256

# A2A TaskState values we use.
SUBMITTED = "submitted"
WORKING = "working"
COMPLETED = "completed"
CANCELED = "canceled"
FAILED = "failed"
TERMINAL = frozenset({COMPLETED, CANCELED, FAILED})


class A2AError(Exception):
    """A JSON-RPC error carrying a ``code``/``message`` (and optional ``data``)."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


@dataclass
class A2ATask:
    """One A2A task = one mapped Hermes tool call, plus its state history."""

    id: str
    context_id: str
    tool: str
    arguments: Dict[str, Any]
    state: str = SUBMITTED
    result: Optional[str] = None
    error: Optional[str] = None
    history: List[Dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize as an A2A ``Task`` object (camelCase)."""
        status: Dict[str, Any] = {"state": self.state, "timestamp": _now_iso()}
        if self.error:
            status["message"] = _agent_text(self.error)
        task: Dict[str, Any] = {
            "id": self.id,
            "contextId": self.context_id,
            "kind": "task",
            "status": status,
            "history": list(self.history),
            "artifacts": _artifacts(self),
        }
        return task


class TaskStore:
    """Thread-safe, bounded in-memory task store (FIFO eviction)."""

    def __init__(self, max_tasks: int = MAX_TASKS) -> None:
        self._tasks: "OrderedDict[str, A2ATask]" = OrderedDict()
        self._lock = Lock()
        self._max = max_tasks

    def put(self, task: A2ATask) -> None:
        with self._lock:
            self._tasks[task.id] = task
            self._tasks.move_to_end(task.id)
            while len(self._tasks) > self._max:
                self._tasks.popitem(last=False)

    def get(self, task_id: str) -> Optional[A2ATask]:
        with self._lock:
            return self._tasks.get(task_id)


# Process-wide default store. Tests may pass their own.
_STORE = TaskStore()


# --------------------------------------------------------------------------
# JSON-RPC entry points (consumed by the web_server route)
# --------------------------------------------------------------------------


async def handle_rpc(payload: Any, store: Optional[TaskStore] = None) -> Dict[str, Any]:
    """Handle a non-streaming JSON-RPC call, returning a response envelope.

    Supports ``message/send`` (create + run a task to completion),
    ``tasks/get`` and ``tasks/cancel``.
    """
    store = store or _STORE
    rid, method, params, err = _parse_envelope(payload)
    if err is not None:
        return _rpc_error(rid, err)
    try:
        if method == "message/send":
            task = _new_task(params)
            await _execute(task, store)
            return _rpc_result(rid, task.to_dict())
        if method == "tasks/get":
            return _rpc_result(rid, _lookup(params, store).to_dict())
        if method == "tasks/cancel":
            task = _lookup(params, store)
            if task.state not in TERMINAL:
                _set_state(task, CANCELED)
            return _rpc_result(rid, task.to_dict())
        raise A2AError(METHOD_NOT_FOUND, f"unknown method: {method}")
    except A2AError as exc:
        return _rpc_error(rid, exc)
    except Exception:  # pragma: no cover - defensive: never leak a traceback
        logger.exception("A2A: internal error handling %r", method)
        return _rpc_error(rid, A2AError(INTERNAL_ERROR, "internal error"))


async def sse_stream(
    payload: Any, store: Optional[TaskStore] = None
) -> AsyncIterator[str]:
    """Handle ``message/stream``, yielding SSE frames for each state transition.

    Emits ``submitted`` -> ``working`` -> terminal (``completed``/``failed``),
    each as a JSON-RPC result wrapping an A2A ``status-update`` event. The
    generator ALWAYS terminates (terminal frame then return), so the stream is
    never left open.
    """
    store = store or _STORE
    rid, method, params, err = _parse_envelope(payload)
    if err is not None:
        yield _sse(_rpc_error(rid, err))
        return
    if method != "message/stream":
        yield _sse(
            _rpc_error(rid, A2AError(METHOD_NOT_FOUND, f"unknown method: {method}"))
        )
        return
    try:
        task = _new_task(params)
    except A2AError as exc:
        yield _sse(_rpc_error(rid, exc))
        return

    store.put(task)
    yield _sse(_rpc_result(rid, _status_event(task, final=False)))
    _set_state(task, WORKING)
    yield _sse(_rpc_result(rid, _status_event(task, final=False)))
    await _run(task)
    yield _sse(_rpc_result(rid, _status_event(task, final=True)))


def parse_error_response() -> Dict[str, Any]:
    """Envelope for an unparseable request body (id is null per JSON-RPC)."""
    return _rpc_error(None, A2AError(PARSE_ERROR, "parse error"))


# --------------------------------------------------------------------------
# Task creation / execution
# --------------------------------------------------------------------------


def _new_task(params: Dict[str, Any]) -> A2ATask:
    """Build a task from ``message/send`` params, authorizing the mapped tool."""
    message = params.get("message")
    tool, args = _extract_tool_call(message)
    _authorize_tool(tool)
    context_id = ""
    if isinstance(message, dict):
        context_id = str(message.get("contextId") or "")
    context_id = context_id or str(params.get("contextId") or "") or _new_id()
    return A2ATask(id=_new_id(), context_id=context_id, tool=tool, arguments=args)


async def _execute(task: A2ATask, store: TaskStore) -> None:
    """Store the task, mark it working, run its tool to a terminal state."""
    store.put(task)
    _set_state(task, WORKING)
    await _run(task)


async def _run(task: A2ATask) -> None:
    """Dispatch the task's tool in a worker thread under a wall-clock timeout."""
    try:
        ok, payload = await asyncio.wait_for(
            asyncio.to_thread(_run_tool, task), TOOL_TIMEOUT_S
        )
    except asyncio.TimeoutError:
        _finish(task, FAILED, error=f"tool timed out after {int(TOOL_TIMEOUT_S)}s")
        return
    except Exception as exc:  # pragma: no cover - defensive
        _finish(task, FAILED, error=f"{type(exc).__name__}: {exc}")
        return
    if ok:
        _finish(task, COMPLETED, result=payload)
    else:
        _finish(task, FAILED, error=payload)


def _finish(
    task: A2ATask,
    state: str,
    *,
    result: Optional[str] = None,
    error: Optional[str] = None,
) -> None:
    """Apply a terminal outcome UNLESS the task already reached a terminal state.

    A concurrent ``tasks/cancel`` may have moved the task to ``canceled`` while
    its tool was still running in the worker thread (which can't be preempted).
    Guarding here keeps that cancellation authoritative instead of letting the
    late tool result overwrite it.
    """
    if task.state in TERMINAL:
        return
    if result is not None:
        task.result = result
    if error is not None:
        task.error = error
    _set_state(task, state)


def _run_tool(task: A2ATask) -> Tuple[bool, str]:
    """Execute the tool via the registry dispatch seam. Returns (ok, payload).

    ``registry.dispatch`` already catches handler exceptions, sanitizes error
    strings, bridges async handlers, and returns a JSON string -- so this is
    the single seam through which A2A reaches tools. No new tool logic here.
    """
    from tools.registry import registry

    if registry.get_entry(task.tool) is None:
        return False, f"tool not available: {task.tool}"
    raw = registry.dispatch(task.tool, dict(task.arguments))
    dispatch_err = _dispatch_error(raw)
    if dispatch_err is not None:
        return False, dispatch_err
    return True, raw


def _dispatch_error(raw: str) -> Optional[str]:
    """Return the error string if ``raw`` is a ``{"error": ...}``-only envelope."""
    try:
        obj = json.loads(raw)
    except Exception:
        return None
    if isinstance(obj, dict) and set(obj) == {"error"}:
        return str(obj["error"])
    return None


# --------------------------------------------------------------------------
# Parsing / authorization / serialization helpers
# --------------------------------------------------------------------------


def _parse_envelope(
    payload: Any,
) -> Tuple[Any, str, Dict[str, Any], Optional[A2AError]]:
    """Validate the JSON-RPC envelope. Returns (id, method, params, error)."""
    if not isinstance(payload, dict):
        return None, "", {}, A2AError(INVALID_REQUEST, "request must be a JSON object")
    rid = payload.get("id")
    if payload.get("jsonrpc") != "2.0":
        return rid, "", {}, A2AError(INVALID_REQUEST, "jsonrpc must be '2.0'")
    method = payload.get("method")
    if not isinstance(method, str) or not method:
        return (
            rid,
            "",
            {},
            A2AError(INVALID_REQUEST, "method must be a non-empty string"),
        )
    params = payload.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return rid, method, {}, A2AError(INVALID_PARAMS, "params must be an object")
    return rid, method, params, None


def _extract_tool_call(message: Any) -> Tuple[str, Dict[str, Any]]:
    """Pull ``(tool, arguments)`` out of an A2A message.

    Two carriers, in priority order: a ``data`` part
    (``{"kind": "data", "data": {"tool": ..., "arguments": {...}}}``) or the
    message-level ``metadata``. Both are explicit structured mappings -- we do
    NOT parse free-text into a tool call, which would be an injection surface.
    """
    if not isinstance(message, dict):
        raise A2AError(INVALID_PARAMS, "'message' must be an object")
    for part in message.get("parts") or []:
        if isinstance(part, dict) and part.get("kind") == "data":
            data = part.get("data")
            if isinstance(data, dict) and "tool" in data:
                return _validate_call(data)
    metadata = message.get("metadata")
    if isinstance(metadata, dict) and "tool" in metadata:
        return _validate_call(metadata)
    raise A2AError(
        INVALID_PARAMS,
        "message carries no tool mapping (expected a data part or metadata "
        "with a 'tool' field)",
    )


def _validate_call(data: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    tool = data.get("tool")
    if not isinstance(tool, str) or not tool:
        raise A2AError(INVALID_PARAMS, "'tool' must be a non-empty string")
    args = data.get("arguments", {})
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise A2AError(INVALID_PARAMS, "'arguments' must be an object")
    return tool, args


def _authorize_tool(tool: str) -> None:
    """Reject any tool not advertised on the Agent Card (honours ``exclude``)."""
    if tool not in _advertised_tools():
        # Same code/shape as a genuinely unknown tool: no existence oracle.
        raise A2AError(METHOD_NOT_FOUND, f"tool not found: {tool}")


def _advertised_tools() -> set:
    """The set of tool ids currently advertised on the Agent Card (no skills)."""
    from hermes_cli.a2a import get_discovery_snapshot

    _config, capabilities = get_discovery_snapshot()
    return {c.id for c in capabilities if not c.id.startswith("skill:")}


def _set_state(task: A2ATask, state: str) -> None:
    """Record the current status in history, then move to ``state``."""
    task.history.append({"state": task.state, "timestamp": _now_iso()})
    task.state = state


def _lookup(params: Dict[str, Any], store: TaskStore) -> A2ATask:
    task_id = params.get("id")
    if not isinstance(task_id, str) or not task_id:
        raise A2AError(INVALID_PARAMS, "'id' is required")
    task = store.get(task_id)
    if task is None:
        raise A2AError(TASK_NOT_FOUND, f"task not found: {task_id}")
    return task


def _status_event(task: A2ATask, *, final: bool) -> Dict[str, Any]:
    """An A2A ``status-update`` streaming event for ``task``'s current state."""
    status: Dict[str, Any] = {"state": task.state, "timestamp": _now_iso()}
    if final and task.error:
        status["message"] = _agent_text(task.error)
    event: Dict[str, Any] = {
        "taskId": task.id,
        "contextId": task.context_id,
        "kind": "status-update",
        "status": status,
        "final": final,
    }
    if final:
        event["artifacts"] = _artifacts(task)
    return event


def _artifacts(task: A2ATask) -> List[Dict[str, Any]]:
    if task.result is None:
        return []
    return [
        {
            "artifactId": f"{task.id}-result",
            "parts": [{"kind": "text", "text": task.result}],
        }
    ]


def _agent_text(text: str) -> Dict[str, Any]:
    return {"role": "agent", "parts": [{"kind": "text", "text": text}]}


def _rpc_result(rid: Any, result: Dict[str, Any]) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _rpc_error(rid: Any, err: A2AError) -> Dict[str, Any]:
    error: Dict[str, Any] = {"code": err.code, "message": err.message}
    if err.data is not None:
        error["data"] = err.data
    return {"jsonrpc": "2.0", "id": rid, "error": error}


def _sse(obj: Dict[str, Any]) -> str:
    return f"data: {json.dumps(obj)}\n\n"


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _new_id() -> str:
    return uuid.uuid4().hex
