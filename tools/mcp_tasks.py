"""MCP Tasks extension client support (issue #2285, MCP 2026-07-28 spec).

Drives long-running MCP operations that answer ``tools/call`` with a durable
``taskId`` (Tasks extension ``io.modelcontextprotocol/tasks``) via
``tasks/get`` (poll), ``tasks/result`` (fetch payload), ``tasks/cancel``
(abort). Uses the stable ``BaseSession.send_request`` with typed MCP messages
rather than the deprecated ``session.experimental`` surface (removed in mcp
2.0 / SEP-1686). Pure-async and transport-agnostic: accepts any session
exposing ``send_request`` (real ClientSession or a test double).
"""

from __future__ import annotations

from typing import Any, Optional, Type, TypeVar

try:  # mcp is optional — module must import cleanly when absent
    import mcp.types as _t
except Exception:  # pragma: no cover
    _t = None

T = TypeVar("T", bound=Any)
_TERMINAL = frozenset({"completed", "failed", "cancelled"})


def _types() -> Any:
    if _t is None:
        raise RuntimeError("MCP Tasks support requires the 'mcp' package")
    return _t


def _is_terminal(status: str) -> bool:
    return status in _TERMINAL


async def get_task(session: Any, task_id: str) -> Any:
    """Return the current ``GetTaskResult`` (status + metadata)."""
    t = _types()
    return await session.send_request(
        t.ClientRequest(t.GetTaskRequest(params=t.GetTaskRequestParams(taskId=task_id))),
        t.GetTaskResult,
    )


async def get_task_result(session: Any, task_id: str, result_type: Optional[Type[T]] = None) -> T:
    """Fetch the final payload of a completed task (defaults to CallToolResult)."""
    t = _types()
    payload_type: Type[T] = result_type or t.CallToolResult  # type: ignore[assignment]
    return await session.send_request(
        t.ClientRequest(t.GetTaskPayloadRequest(params=t.GetTaskPayloadRequestParams(taskId=task_id))),
        payload_type,
    )


async def cancel_task(session: Any, task_id: str) -> Any:
    """Request cancellation of a running task, returning updated state."""
    t = _types()
    return await session.send_request(
        t.ClientRequest(t.CancelTaskRequest(params=t.CancelTaskRequestParams(taskId=task_id))),
        t.CancelTaskResult,
    )


async def poll_task(
    session: Any,
    task_id: str,
    *,
    timeout: float = 300.0,
    poll_interval: float = 0.5,
) -> Any:
    """Poll until terminal (completed/failed/cancelled), honoring the server's
    suggested ``pollInterval``; raises TimeoutError otherwise."""
    import asyncio
    import time

    deadline = time.monotonic() + timeout
    interval = poll_interval
    status = None
    while time.monotonic() < deadline:
        status = await get_task(session, task_id)
        if _is_terminal(str(getattr(status, "status", ""))):
            return status
        hint = getattr(status, "pollInterval", None)
        if hint is not None:
            interval = max(0.05, float(hint) / 1000.0)
        await asyncio.sleep(interval)
    raise TimeoutError(
        f"MCP task {task_id} did not finish within {timeout:.0f}s "
        f"(last status: {getattr(status, 'status', 'unknown')!r})"
    )
