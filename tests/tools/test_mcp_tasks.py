"""Tests for MCP Tasks extension client support (#2285). Uses a fake session
exposing ``send_request`` so the helpers run without a live server."""

from __future__ import annotations

import asyncio

import pytest
import mcp.types as t

from tools.mcp_tasks import _is_terminal, cancel_task, get_task, get_task_result, poll_task


def _task(tid: str, status: str, poll_interval=None) -> dict:
    return {
        "taskId": tid,
        "status": status,
        "createdAt": "2026-01-01T00:00:00Z",
        "lastUpdatedAt": "2026-01-01T00:00:00Z",
        "ttl": 600000,
        "pollInterval": poll_interval,
    }


class FakeSession:
    def __init__(self, tasks: dict, payload=None, result_type=None):
        self.tasks, self.payload, self.result_type, self.requests = tasks, payload, result_type, []

    async def send_request(self, client_request, result_type=None):
        root = client_request.root
        tid = getattr(root.params or {}, "taskId", None)
        self.requests.append(root.method)
        if root.method == "tasks/get":
            return t.GetTaskResult.model_validate(self.tasks[tid])
        if root.method == "tasks/result":
            return self.result_type.model_validate(self.payload) if self.result_type else self.payload
        if root.method == "tasks/cancel":
            self.tasks[tid] = _task(tid, "cancelled")
            return t.CancelTaskResult.model_validate(self.tasks[tid])
        raise AssertionError(root.method)


def test_is_terminal() -> None:
    assert _is_terminal("completed") and _is_terminal("failed") and _is_terminal("cancelled")
    assert not _is_terminal("working") and not _is_terminal("input_required")


def test_get_task_returns_status() -> None:
    sess = FakeSession({"t1": _task("t1", "working")})
    result = asyncio.run(get_task(sess, "t1"))
    assert result.taskId == "t1" and result.status == "working"
    assert sess.requests == ["tasks/get"]


def test_poll_task_waits_until_terminal() -> None:
    calls = {"n": 0}

    class PollingSession(FakeSession):
        async def send_request(self, client_request, result_type=None):
            if client_request.root.method == "tasks/get":
                calls["n"] += 1
                return t.GetTaskResult.model_validate(_task("t1", "completed" if calls["n"] >= 2 else "working"))
            raise AssertionError(client_request.root.method)

    result = asyncio.run(poll_task(PollingSession({}), "t1", timeout=5, poll_interval=0.01))
    assert result.status == "completed" and calls["n"] == 2


def test_poll_task_honors_poll_interval_hint() -> None:
    class HintSession(FakeSession):
        async def send_request(self, client_request, result_type=None):
            if client_request.root.method == "tasks/get":
                return t.GetTaskResult.model_validate(_task("t1", "completed", 250))
            raise AssertionError(client_request.root.method)

    assert asyncio.run(poll_task(HintSession({}), "t1", timeout=5)).status == "completed"


def test_poll_task_times_out() -> None:
    class ForeverSession(FakeSession):
        async def send_request(self, client_request, result_type=None):
            if client_request.root.method == "tasks/get":
                return t.GetTaskResult.model_validate(_task("t1", "working"))
            raise AssertionError(client_request.root.method)

    with pytest.raises(TimeoutError):
        asyncio.run(poll_task(ForeverSession({}), "t1", timeout=0.1, poll_interval=0.01))


def test_get_task_result_returns_payload() -> None:
    payload = t.CallToolResult.model_validate({"content": [{"type": "text", "text": "done"}], "isError": False})
    sess = FakeSession({}, payload=payload, result_type=t.CallToolResult)
    result = asyncio.run(get_task_result(sess, "t1", t.CallToolResult))
    assert sess.requests == ["tasks/result"] and result.content[0].text == "done"


def test_cancel_task_returns_cancelled() -> None:
    sess = FakeSession({"t1": _task("t1", "working")})
    result = asyncio.run(cancel_task(sess, "t1"))
    assert sess.requests == ["tasks/cancel"] and result.status == "cancelled"
