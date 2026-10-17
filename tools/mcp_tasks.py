# -*- coding: utf-8 -*-
"""MCP Tasks extension support for long-running operations (MCP 2026-07-28 spec, issue #2285).

Implements the MCP Tasks extension (io.modelcontextprotocol/tasks):
1. Detects task responses (taskId) returned by MCP servers during tools/call.
2. Polls tasks/get until completion, surfacing progress updates.
3. Retrieves final results via tasks/result.
4. Supports graceful cancellation via tasks/cancel upon agent interrupt.
5. Provides in-process TaskManager to wrap long-running operations as MCP Tasks.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Coroutine, Dict, List, Optional, Tuple, Union

logger = logging.getLogger(__name__)

__all__ = [
    "MCPTask",
    "MCPTaskManager",
    "cancel_task",
    "extract_task_info",
    "get_task",
    "get_task_result",
    "poll_task_to_completion",
    "send_mcp_rpc_request",
]

_TERMINAL_STATUSES = {
    "completed",
    "success",
    "done",
    "failed",
    "error",
    "cancelled",
    "canceled",
}

#: Consecutive poll failures tolerated before the loop gives up (#3499). Bounded so a
#: non-transient failure stops the loop instead of re-issuing the same request every
#: ``poll_interval`` for the whole window (598 identical warnings in one window).
_POLL_MAX_CONSECUTIVE_ERRORS = 5

#: Fraction of the shared exponential backoff added/removed as jitter, so a fleet of
#: pollers that fail together does not re-issue in lockstep.
_POLL_JITTER_RATIO = 0.25


@dataclass
class MCPTask:
    """Represents an MCP Task tracked under the MCP 2026-07-28 Tasks extension."""

    task_id: str
    status: str = (
        "working"  # working, running, in_progress, completed, failed, cancelled
    )
    progress: Optional[float] = None
    total: Optional[float] = None
    message: str = ""
    result: Any = None
    error: Optional[str] = None
    server_name: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    raw_payload: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        return self.status.lower() in _TERMINAL_STATUSES

    @property
    def is_successful(self) -> bool:
        return self.status.lower() in {"completed", "success", "done"}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "taskId": self.task_id,
            "status": self.status,
            "progress": self.progress,
            "total": self.total,
            "message": self.message,
            "result": self.result,
            "error": self.error,
            "server_name": self.server_name,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any], server_name: str = "") -> MCPTask:
        # Handle both camelCase and snake_case keys
        tid = str(data.get("taskId") or data.get("task_id") or data.get("id") or "")
        status = str(data.get("status") or "working")
        progress = data.get("progress")
        total = data.get("total")
        message = str(data.get("message") or "")
        result = data.get("result")
        error = data.get("error")
        created_at = float(data.get("created_at") or time.time())
        updated_at = float(data.get("updated_at") or time.time())

        return cls(
            task_id=tid,
            status=status,
            progress=float(progress) if progress is not None else None,
            total=float(total) if total is not None else None,
            message=message,
            result=result,
            error=str(error) if error is not None else None,
            server_name=server_name,
            created_at=created_at,
            updated_at=updated_at,
            raw_payload=data,
        )


def extract_task_info(result: Any) -> Optional[Tuple[str, str]]:
    """Extract (task_id, status) from a tool call result if it represents a Tasks-extension response.

    Returns None if result is a regular tool result without a task handle.
    """
    if result is None:
        return None

    # 1. Dict check
    if isinstance(result, dict):
        if "taskId" in result or "task_id" in result:
            tid = str(result.get("taskId") or result.get("task_id"))
            status = str(result.get("status", "working"))
            return tid, status
        if "task" in result and isinstance(result["task"], dict):
            task_obj = result["task"]
            tid = str(
                task_obj.get("taskId") or task_obj.get("task_id") or task_obj.get("id")
            )
            status = str(task_obj.get("status", "working"))
            if tid:
                return tid, status

    # 2. CallToolResult or object with attributes
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        info = extract_task_info(structured)
        if info:
            return info

    content = getattr(result, "content", None)
    if isinstance(content, list):
        for block in content:
            txt = getattr(block, "text", None)
            if txt and isinstance(txt, str) and ("taskId" in txt or "task_id" in txt):
                try:
                    parsed = json.loads(txt)
                    info = extract_task_info(parsed)
                    if info:
                        return info
                except Exception:
                    pass

    # 3. JSON string check
    if isinstance(result, str) and ("taskId" in result or "task_id" in result):
        try:
            parsed = json.loads(result)
            info = extract_task_info(parsed)
            if info:
                return info
        except Exception:
            pass

    return None


async def send_mcp_rpc_request(
    session: Any, method: str, params: Dict[str, Any]
) -> Any:
    """Send a generic JSON-RPC request over the MCP session."""
    if session is None:
        raise ValueError("MCP session is not connected.")

    # 1. If session has send_request (official MCP SDK pattern)
    if hasattr(session, "send_request"):
        try:
            return await session.send_request(
                {
                    "jsonrpc": "2.0",
                    "method": method,
                    "params": params,
                    "id": 0,
                },
                Any,
            )
        except TypeError:
            # Fallback without result_type if send_request takes 1 arg
            return await session.send_request({
                "jsonrpc": "2.0",
                "method": method,
                "params": params,
                "id": 0,
            })

    # 2. Direct named method fallback (e.g. session.tasks_get)
    clean_method = method.replace("/", "_")
    if hasattr(session, clean_method):
        fn = getattr(session, clean_method)
        if asyncio.iscoroutinefunction(fn):
            return await fn(**params)
        return fn(**params)

    # 3. Underlying transport request
    if hasattr(session, "_request") and callable(session._request):
        return await session._request(method, params)

    raise NotImplementedError(
        f"Session {type(session).__name__} does not support sending RPC method {method}"
    )


async def get_task(session: Any, task_id: str, server_name: str = "") -> MCPTask:
    """Query task status and progress via tasks/get."""
    resp = await send_mcp_rpc_request(session, "tasks/get", {"taskId": task_id})
    if isinstance(resp, dict):
        if "task" in resp and isinstance(resp["task"], dict):
            return MCPTask.from_dict(resp["task"], server_name=server_name)
        return MCPTask.from_dict(resp, server_name=server_name)
    return MCPTask(task_id=task_id, status="working", server_name=server_name)


async def get_task_result(session: Any, task_id: str) -> Any:
    """Fetch the final output of a completed task via tasks/result."""
    resp = await send_mcp_rpc_request(session, "tasks/result", {"taskId": task_id})
    if isinstance(resp, dict):
        if "result" in resp:
            return resp["result"]
        return resp
    return resp


async def cancel_task(session: Any, task_id: str, reason: str = "") -> bool:
    """Cancel an active task via tasks/cancel."""
    try:
        resp = await send_mcp_rpc_request(
            session, "tasks/cancel", {"taskId": task_id, "reason": reason}
        )
        if isinstance(resp, dict):
            return bool(resp.get("success", True))
        return True
    except Exception as exc:
        logger.warning("Failed to cancel MCP task %s: %s", task_id, exc)
        return False


def _is_non_retryable_poll_error(exc: BaseException) -> bool:
    """True for a poll failure that cannot be fixed by re-issuing the same request (#3499).

    A response-shape mismatch - a plain ``dict`` returned where a model object is
    expected, or a server that does not implement ``tasks/get`` at all - raises the
    same ``AttributeError`` / ``NotImplementedError`` / ``TypeError`` on every
    attempt. Those are surfaced once per task instead of being retried.
    """
    return isinstance(exc, (AttributeError, NotImplementedError, TypeError))


def _poll_backoff_delay(consecutive_errors: int) -> float:
    """Delay before retry N of a failing poll: shared exponential backoff + jitter.

    Reuses the same retry policy as core tool calls
    (:func:`tools.recovery_strategy_dispatcher.backoff_seconds_for`: 1s, 2s, 4s, ...,
    capped) so the poll path does not grow a second, divergent policy. The jitter is
    bounded to +/- ``_POLL_JITTER_RATIO``; successive bands never overlap, so the
    delay is still strictly increasing in ``consecutive_errors``.
    """
    from tools.recovery_strategy_dispatcher import backoff_seconds_for

    base = backoff_seconds_for(max(0, int(consecutive_errors) - 1))
    return base * (1.0 + random.uniform(-_POLL_JITTER_RATIO, _POLL_JITTER_RATIO))


async def poll_task_to_completion(
    session: Any,
    task_id: str,
    server_name: str = "",
    poll_interval: float = 0.5,
    max_wait_sec: float = 300.0,
    on_progress: Optional[Callable[[MCPTask], None]] = None,
    max_consecutive_errors: int = _POLL_MAX_CONSECUTIVE_ERRORS,
) -> MCPTask:
    """Poll tasks/get until the task terminates, then retrieve and attach its final result.

    Handles cancellation, timeouts, and errors according to the MCP 2026-07-28 spec.

    A failing poll is retried with the shared exponential-backoff policy
    (:func:`_poll_backoff_delay`) and bounded by *max_consecutive_errors*. The first
    failure in a streak is logged once, a non-retryable shape error stops the loop
    immediately, and a streak that reaches the cap returns a failed task - so a
    permanently erroring task is never re-issued every ``poll_interval`` for the whole
    window (issue #3499: 598 identical warnings at ~500 ms in one window).
    """
    start_time = time.time()
    consecutive_errors = 0

    while True:
        try:
            task = await get_task(session, task_id, server_name=server_name)
        except Exception as exc:
            consecutive_errors += 1
            non_retryable = _is_non_retryable_poll_error(exc)
            if non_retryable or consecutive_errors >= max_consecutive_errors:
                logger.warning(
                    "Giving up polling task %s on server %s after %d consecutive "
                    "error(s)%s: %s",
                    task_id,
                    server_name,
                    consecutive_errors,
                    " (non-retryable response shape)"
                    if non_retryable
                    else " (attempt cap reached)",
                    exc,
                )
                return MCPTask(
                    task_id=task_id,
                    status="failed",
                    server_name=server_name,
                    message=str(exc),
                    error=f"{type(exc).__name__}: {exc}",
                )
            if consecutive_errors == 1:
                logger.warning(
                    "Error polling task %s on server %s; retrying with backoff "
                    "(cap %d consecutive attempts): %s",
                    task_id,
                    server_name,
                    max_consecutive_errors,
                    exc,
                )
            # Never sleep past the poll deadline: an error streak must not overshoot it.
            remaining = max_wait_sec - (time.time() - start_time)
            await asyncio.sleep(
                max(0.0, min(_poll_backoff_delay(consecutive_errors), remaining))
            )
            continue

        # A healthy poll clears the streak so a late failure gets a fresh budget.
        consecutive_errors = 0

        if on_progress:
            try:
                on_progress(task)
            except Exception as e:
                logger.debug("on_progress callback error: %s", e)

        if task.is_terminal:
            if task.is_successful and task.result is None:
                try:
                    final_result = await get_task_result(session, task_id)
                    task.result = final_result
                except Exception as exc:
                    logger.debug(
                        "tasks/result call error for %s: %s (using task payload)",
                        task_id,
                        exc,
                    )
            return task

        if time.time() - start_time >= max_wait_sec:
            logger.warning(
                "Task %s exceeded max wait timeout (%ss); attempting cancel...",
                task_id,
                max_wait_sec,
            )
            await cancel_task(session, task_id, reason="Client timeout exceeded")
            task.status = "failed"
            task.error = f"Task timed out after {max_wait_sec}s"
            return task

        await asyncio.sleep(poll_interval)


class MCPTaskManager:
    """In-process manager to wrap long-running operations as MCP Tasks."""

    def __init__(self) -> None:
        self._tasks: Dict[str, MCPTask] = {}
        self._async_handles: Dict[str, asyncio.Task[Any]] = {}

    def create_task(
        self,
        coro_or_fn: Union[Callable[..., Coroutine[Any, Any, Any]], Callable[..., Any]],
        *args: Any,
        **kwargs: Any,
    ) -> MCPTask:
        """Create and spawn a background task."""
        tid = f"task_{uuid.uuid4().hex[:12]}"
        task = MCPTask(task_id=tid, status="working")
        self._tasks[tid] = task

        async def _runner() -> None:
            try:
                if asyncio.iscoroutinefunction(coro_or_fn):
                    res = await coro_or_fn(*args, **kwargs)
                else:
                    res = coro_or_fn(*args, **kwargs)
                task.status = "completed"
                task.result = res
                task.updated_at = time.time()
            except asyncio.CancelledError:
                task.status = "cancelled"
                task.updated_at = time.time()
            except Exception as exc:
                task.status = "failed"
                task.error = str(exc)
                task.updated_at = time.time()

        handle = asyncio.create_task(_runner())
        self._async_handles[tid] = handle
        return task

    def get_task(self, task_id: str) -> Optional[MCPTask]:
        return self._tasks.get(task_id)

    def cancel_task(self, task_id: str, reason: str = "") -> bool:
        handle = self._async_handles.get(task_id)
        if handle and not handle.done():
            handle.cancel()
            task = self._tasks.get(task_id)
            if task:
                task.status = "cancelled"
                task.message = reason
                task.updated_at = time.time()
            return True
        return False
