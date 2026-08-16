# -*- coding: utf-8 -*-
"""Tests for CodeAct-style deterministic tool-call coalescing (Issue #2485, Slice A)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch
import pytest

from evolution.lib.tool_coalescer import (
    CoalescedExecutionResult,
    CoalescedItemResult,
    ToolCallCoalescer,
    ToolCallSpec,
)
from run_agent import AIAgent


class TestToolCallCoalescer:
    def test_can_coalesce_predicate(self):
        # Single tool call should not coalesce
        assert ToolCallCoalescer.can_coalesce([{"name": "read_file"}]) is False

        # Multiple safe tools should coalesce
        safe_batch = [
            {"name": "read_file", "arguments": {"path": "a.txt"}},
            {"name": "web_search", "arguments": {"query": "test"}},
            {"name": "mcp_custom_tool", "arguments": {"param": 1}},
        ]
        assert ToolCallCoalescer.can_coalesce(safe_batch) is True

        # Batch with unsafe/interactive tool should not coalesce
        unsafe_batch = [
            {"name": "read_file", "arguments": {"path": "a.txt"}},
            {"name": "terminal", "arguments": {"command": "rm -rf /"}},
        ]
        assert ToolCallCoalescer.can_coalesce(unsafe_batch) is False

    def test_generate_codeact_script(self):
        calls = [
            ToolCallSpec(name="read_file", arguments={"path": "main.py"}, call_id="c1"),
            ToolCallSpec(
                name="web_search", arguments={"query": "python"}, call_id="c2"
            ),
        ]
        script = ToolCallCoalescer.generate_codeact_script(calls)
        assert "# CodeAct coalesced execution bundle" in script
        assert "call_tool('read_file'" in script
        assert "call_tool('web_search'" in script
        assert "'call_id': 'c1'" in script
        assert "'call_id': 'c2'" in script

    def test_coalesce_and_execute_success(self):
        executed_calls = []

        def mock_handler(name, args):
            executed_calls.append((name, args))
            return f"result_for_{name}"

        calls = [
            {"name": "read_file", "arguments": {"path": "foo.py"}, "id": "call_1"},
            {"name": "web_search", "arguments": {"query": "hermes"}, "id": "call_2"},
        ]

        result = ToolCallCoalescer.coalesce_and_execute(calls, mock_handler)
        assert isinstance(result, CoalescedExecutionResult)
        assert result.call_count == 2
        assert result.coalesced_round_trip is True
        assert len(result.results) == 2
        assert result.results[0].result == "result_for_read_file"
        assert result.results[1].result == "result_for_web_search"
        assert len(executed_calls) == 2

    def test_coalesce_and_execute_with_error(self):
        def faulty_handler(name, args):
            if name == "fail_tool":
                raise RuntimeError("Service unavailable")
            return "ok"

        calls = [
            {"name": "good_tool", "arguments": {}, "id": "c1"},
            {"name": "fail_tool", "arguments": {}, "id": "c2"},
        ]

        result = ToolCallCoalescer.coalesce_and_execute(calls, faulty_handler)
        assert result.call_count == 2
        assert result.results[0].success is True
        assert result.results[1].success is False
        assert "Service unavailable" in result.results[1].error

    def test_to_tool_messages(self):
        result = CoalescedExecutionResult(
            results=[
                CoalescedItemResult(call_id="c1", name="tool_a", result="Output A"),
                CoalescedItemResult(call_id="c2", name="tool_b", result={"key": "val"}),
            ],
            total_duration_ms=15.0,
            call_count=2,
        )
        msgs = result.to_tool_messages()
        assert len(msgs) == 2
        assert msgs[0] == {
            "role": "tool",
            "tool_call_id": "c1",
            "name": "tool_a",
            "content": "Output A",
        }
        assert msgs[1]["role"] == "tool"
        assert msgs[1]["tool_call_id"] == "c2"
        assert '"key": "val"' in msgs[1]["content"]


class TestAIAgentToolCoalescingIntegration:
    def test_agent_execute_tool_calls_coalesced(self):
        agent = AIAgent(
            api_key="mock-key",
            base_url="http://localhost:8080/v1",
            model="test-model",
            quiet_mode=True,
            session_id="test_coalesce_sess",
        )

        tc1 = MagicMock(name="tc1")
        tc1.name = "read_file"
        tc1.arguments = '{"path": "test.txt"}'
        tc1.id = "c1"

        tc2 = MagicMock(name="tc2")
        tc2.name = "web_search"
        tc2.arguments = '{"query": "docs"}'
        tc2.id = "c2"

        mock_msg = MagicMock()
        mock_msg.tool_calls = [tc1, tc2]

        messages = []
        with patch(
            "run_agent.handle_function_call",
            side_effect=lambda name, args, task_id: f"mock_{name}",
        ):
            res = agent._execute_tool_calls_coalesced(mock_msg, messages, "task-1")

        assert res.call_count == 2
        assert len(messages) == 2
        assert messages[0]["role"] == "tool"
        assert messages[0]["content"] == "mock_read_file"
        assert messages[1]["role"] == "tool"
        assert messages[1]["content"] == "mock_web_search"

    def test_agent_coalesce_and_execute_tool_calls_direct(self):
        agent = AIAgent(
            api_key="mock-key",
            base_url="http://localhost:8080/v1",
            model="test-model",
            quiet_mode=True,
            session_id="test_coalesce_sess_direct",
        )
        calls = [
            {"name": "read_file", "arguments": {"path": "README.md"}, "id": "r1"},
            {"name": "context_var", "arguments": {"action": "list"}, "id": "r2"},
        ]
        with patch(
            "run_agent.handle_function_call",
            side_effect=lambda name, args, task_id: f"executed_{name}",
        ):
            res = agent.coalesce_and_execute_tool_calls(calls, task_id="task-direct")

        assert res.call_count == 2
        assert res.results[0].result == "executed_read_file"
        assert res.results[1].result == "executed_context_var"
