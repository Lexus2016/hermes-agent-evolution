# -*- coding: utf-8 -*-
"""Tests for dead-end loop detection (issue #1312)."""

from __future__ import annotations

from agent.loop_guard import detect_dead_end_loop


def _make_tool_call(name: str, args: str, call_id: str = "1") -> dict:
    """Build a minimal assistant message with one tool call."""
    return {
        "role": "assistant",
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": args},
            }
        ],
    }


def _make_tool_result(call_id: str, content: str = "ok") -> dict:
    return {"role": "tool", "tool_call_id": call_id, "content": content}


class TestDetectDeadEndLoop:
    def test_no_messages_returns_none(self):
        assert detect_dead_end_loop([]) is None

    def test_single_call_returns_none(self):
        """One call is not a loop."""
        msgs = [_make_tool_call("terminal", '{"command": "ls"}', "1")]
        assert detect_dead_end_loop(msgs) is None

    def test_identical_call_twice_triggers(self):
        """Two identical (tool, args) = dead-end."""
        msgs = [
            _make_tool_call("terminal", '{"command": "pytest"}', "1"),
            _make_tool_result("1", "FAIL"),
            _make_tool_call("patch", '{"path": "x.py"}', "2"),
            _make_tool_result("2", "ok"),
            _make_tool_call("terminal", '{"command": "pytest"}', "3"),
            _make_tool_result("3", "FAIL"),
        ]
        result = detect_dead_end_loop(msgs)
        assert result is not None
        assert "DEAD-END DETECTED" in result
        assert "terminal" in result
        assert "what_changed_since_last_attempt" in result
        assert "why_expect_different_outcome" in result

    def test_different_args_no_trigger(self):
        """Different arguments = not a dead-end (agent is trying variations)."""
        msgs = [
            _make_tool_call("terminal", '{"command": "pytest tests/a.py"}', "1"),
            _make_tool_result("1", "FAIL"),
            _make_tool_call("terminal", '{"command": "pytest tests/b.py"}', "2"),
            _make_tool_result("2", "FAIL"),
        ]
        assert detect_dead_end_loop(msgs) is None

    def test_three_identical_calls_triggers(self):
        """Three identical calls with intervening edits = dead-end."""
        msgs = [
            _make_tool_call("terminal", '{"command": "npm test"}', "1"),
            _make_tool_result("1", "FAIL"),
            _make_tool_call("patch", '{"path": "a.js"}', "2"),
            _make_tool_result("2", "ok"),
            _make_tool_call("terminal", '{"command": "npm test"}', "3"),
            _make_tool_result("3", "FAIL"),
            _make_tool_call("patch", '{"path": "b.js"}', "4"),
            _make_tool_result("4", "ok"),
            _make_tool_call("terminal", '{"command": "npm test"}', "5"),
            _make_tool_result("5", "FAIL"),
        ]
        result = detect_dead_end_loop(msgs)
        assert result is not None
        assert "3 times" in result

    def test_nudge_contains_structured_justification_prompt(self):
        """The nudge must ask for structured justification (#1312 success criteria)."""
        msgs = [
            _make_tool_call("execute_code", '{"code": "print(1)"}', "1"),
            _make_tool_result("1", "1"),
            _make_tool_call("execute_code", '{"code": "print(1)"}', "2"),
            _make_tool_result("2", "1"),
        ]
        result = detect_dead_end_loop(msgs)
        assert result is not None
        assert "strategy" in result.lower() or "different approach" in result.lower()

    def test_window_respected(self):
        """Calls outside the window should not be counted."""
        # Fill with 60 non-matching messages, then the identical pair
        msgs = []
        for i in range(30):
            cid = str(i)
            msgs.append(_make_tool_call("read_file", f'{{"path": "f{i}.py"}}', cid))
            msgs.append(_make_tool_result(cid))
        msgs.append(_make_tool_call("terminal", '{"command": "ls"}', "100"))
        msgs.append(_make_tool_result("100"))
        msgs.append(_make_tool_call("terminal", '{"command": "ls"}', "101"))
        msgs.append(_make_tool_result("101"))
        # With default window=60, the two identical "ls" calls at the end
        # ARE within the last 60 messages and should trigger.
        result = detect_dead_end_loop(msgs)
        assert result is not None
