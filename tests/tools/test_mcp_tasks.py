# -*- coding: utf-8 -*-
"""Tests for MCP Tasks extension (MCP 2026-07-28 spec, issue #2285)."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tools.mcp_tasks import (
    MCPTask,
    MCPTaskManager,
    cancel_task,
    extract_task_info,
    get_task,
    get_task_result,
    poll_task_to_completion,
    send_mcp_rpc_request,
)
from tools.mcp_tool import _make_tool_handler, _servers


def test_mcp_task_dataclass_and_serialization() -> None:
    task = MCPTask(
        task_id="task_abc123",
        status="working",
        progress=0.45,
        total=1.0,
        message="Executing batch step 2 of 4",
        server_name="analysis_server",
    )

    assert not task.is_terminal
    assert not task.is_successful

    task.status = "completed"
    task.result = {"data": [1, 2, 3]}
    assert task.is_terminal
    assert task.is_successful

    data = task.to_dict()
    assert data["taskId"] == "task_abc123"
    assert data["status"] == "completed"
    assert data["progress"] == 0.45

    loaded = MCPTask.from_dict(data, server_name="analysis_server")
    assert loaded.task_id == task.task_id
    assert loaded.status == task.status
    assert loaded.result == task.result
    assert loaded.server_name == "analysis_server"


def test_extract_task_info() -> None:
    # Direct dict with taskId
    assert extract_task_info({"taskId": "t1", "status": "working"}) == ("t1", "working")
    assert extract_task_info({"task_id": "t2", "status": "running"}) == (
        "t2",
        "running",
    )

    # Nested task dict
    assert extract_task_info({"task": {"taskId": "t3", "status": "in_progress"}}) == (
        "t3",
        "in_progress",
    )

    # JSON string
    assert extract_task_info('{"taskId": "t4", "status": "queued"}') == ("t4", "queued")

    # CallToolResult-like object with content
    mock_block = SimpleNamespace(text='{"taskId": "t5", "status": "working"}')
    mock_res = SimpleNamespace(content=[mock_block], isError=False)
    assert extract_task_info(mock_res) == ("t5", "working")

    # Non-task regular result
    assert extract_task_info({"result": "plain text", "status": "ok"}) is None
    assert extract_task_info("just regular string output") is None
    assert extract_task_info(None) is None


def test_send_mcp_rpc_request_and_primitives() -> None:
    async def _run():
        session = AsyncMock()

        # 1. send_request pattern
        session.send_request = AsyncMock(
            return_value={"task": {"taskId": "t_99", "status": "completed"}}
        )
        task = await get_task(session, "t_99", server_name="srv1")
        assert task.task_id == "t_99"
        assert task.status == "completed"

        session.send_request = AsyncMock(return_value={"result": "done_payload"})
        res = await get_task_result(session, "t_99")
        assert res == "done_payload"

        session.send_request = AsyncMock(return_value={"success": True})
        cancelled = await cancel_task(session, "t_99", reason="User cancel")
        assert cancelled is True

    asyncio.run(_run())


def test_poll_task_to_completion() -> None:
    async def _run():
        session = AsyncMock()

        # Simulate 2 working polls then completed
        poll_responses = [
            {"task": {"taskId": "task_1", "status": "working", "progress": 0.2}},
            {"task": {"taskId": "task_1", "status": "working", "progress": 0.8}},
            {
                "task": {
                    "taskId": "task_1",
                    "status": "completed",
                    "result": {"final": "success"},
                }
            },
        ]

        async def mock_send_request(req, *args, **kwargs):
            method = req.get("method")
            if method == "tasks/get":
                return (
                    poll_responses.pop(0)
                    if poll_responses
                    else {"task": {"taskId": "task_1", "status": "completed"}}
                )
            if method == "tasks/result":
                return {"result": {"final": "success"}}
            return {}

        session.send_request = AsyncMock(side_effect=mock_send_request)

        progress_updates: List[float] = []

        def on_prog(t: MCPTask) -> None:
            if t.progress is not None:
                progress_updates.append(t.progress)

        task = await poll_task_to_completion(
            session,
            "task_1",
            server_name="test_srv",
            poll_interval=0.001,
            max_wait_sec=5.0,
            on_progress=on_prog,
        )

        assert task.task_id == "task_1"
        assert task.is_successful
        assert task.result == {"final": "success"}
        assert progress_updates == [0.2, 0.8]

    asyncio.run(_run())


def test_mcp_task_manager() -> None:
    async def _run():
        manager = MCPTaskManager()

        async def long_op(x: int) -> int:
            await asyncio.sleep(0.01)
            return x * 10

        task = manager.create_task(long_op, 5)
        assert task.status == "working"

        await asyncio.sleep(0.03)
        retrieved = manager.get_task(task.task_id)
        assert retrieved is not None
        assert retrieved.status == "completed"
        assert retrieved.result == 50

    asyncio.run(_run())


def test_mcp_task_manager_cancellation() -> None:
    async def _run():
        manager = MCPTaskManager()

        async def infinite_op() -> None:
            await asyncio.sleep(10.0)

        task = manager.create_task(infinite_op)
        assert task.status == "working"

        ok = manager.cancel_task(task.task_id, reason="Aborted by agent")
        assert ok is True
        await asyncio.sleep(0.01)
        assert task.status == "cancelled"

    asyncio.run(_run())


def test_wired_mcp_tool_call_handler_with_tasks_extension() -> None:
    """Test that _make_tool_handler detects a taskId in tools/call response and drives it to completion."""
    server_name = "test_server_task"
    tool_name = "long_running_analysis"

    # Mock MCP session
    mock_session = MagicMock()

    # Step 1: Initial tools/call returns a task response
    initial_call_result = SimpleNamespace(
        content=[SimpleNamespace(text='{"taskId": "task_abc", "status": "working"}')],
        isError=False,
        structuredContent=None,
    )

    async def mock_call_tool(*args, **kwargs):
        return initial_call_result

    async def mock_send_request(req, *args, **kwargs):
        method = req.get("method")
        if method == "tasks/get":
            return {
                "task": {
                    "taskId": "task_abc",
                    "status": "completed",
                    "result": "Analysis finished!",
                }
            }
        if method == "tasks/result":
            return {"result": "Analysis finished!"}
        return {}

    mock_session.call_tool = mock_call_tool
    mock_session.send_request = mock_send_request

    # Mock server object
    mock_server = SimpleNamespace(
        session=mock_session,
        _rpc_lock=asyncio.Lock(),
        _ready=asyncio.Event(),
        _pending_call_context=None,
        _is_recycled_stdio=lambda: False,
    )
    mock_server._ready.set()
    _servers[server_name] = mock_server

    def fake_run_on_mcp_loop(coro_or_factory, timeout=None):
        coro = coro_or_factory() if callable(coro_or_factory) else coro_or_factory
        return asyncio.run(coro)

    try:
        with (
            patch(
                "tools.mcp_tool._get_connected_server_for_call",
                return_value=mock_server,
            ),
            patch("tools.mcp_tool._run_on_mcp_loop", side_effect=fake_run_on_mcp_loop),
        ):
            handler = _make_tool_handler(server_name, tool_name, tool_timeout=5.0)
            output_json = handler({"dataset": "large.csv"})

        parsed = json.loads(output_json)
        assert "result" in parsed
        assert parsed["result"] == "Analysis finished!"
    finally:
        _servers.pop(server_name, None)
