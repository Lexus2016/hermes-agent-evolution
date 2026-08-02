#!/usr/bin/env python3
"""Tests for the compact_context tool (#1568 SelfCompact)."""

import json
from unittest.mock import MagicMock

from tools.compact_context_tool import (
    COMPACT_CONTEXT_SCHEMA,
    check_compact_context_requirements,
    compact_context_tool,
)


# ─── Schema ──────────────────────────────────────────────────────────────────

def test_schema_name_and_optional_focus():
    assert COMPACT_CONTEXT_SCHEMA["name"] == "compact_context"
    params = COMPACT_CONTEXT_SCHEMA["parameters"]
    assert "focus_topic" in params["properties"]
    assert params["required"] == []


# ─── Availability check ──────────────────────────────────────────────────────

def test_check_requirements_no_agent_returns_true():
    # Schema-level availability — handler guards the real check.
    assert check_compact_context_requirements() is True


def test_check_requirements_agent_without_compressor():
    agent = MagicMock()
    agent.context_compressor = None
    assert check_compact_context_requirements(agent=agent) is False


def test_check_requirements_agent_with_compressor():
    agent = MagicMock()
    agent.context_compressor = MagicMock()
    assert check_compact_context_requirements(agent=agent) is True


# ─── Handler — no agent / no messages ────────────────────────────────────────

def test_no_agent_returns_error():
    result = compact_context_tool(agent=None, messages=[])
    parsed = json.loads(result)
    assert parsed["success"] is False
    assert "not available" in parsed["error"].lower()


def test_no_messages_falls_back_to_session_messages():
    agent = MagicMock()
    agent._session_messages = None
    result = compact_context_tool(agent=agent, messages=None)
    parsed = json.loads(result)
    assert parsed["success"] is False


# ─── Handler — abort path (compressor returns unchanged messages) ────────────

def test_abort_when_compression_does_not_shrink():
    """When _compress_context returns the same list unchanged, the tool
    reports an abort (not a crash) so the model can continue."""
    agent = MagicMock()
    agent._cached_system_prompt = "sys"
    messages = [{"role": "user", "content": "hi"}]
    # Same object returned → no shrink.
    agent._compress_context.return_value = (messages, "sys")

    result = compact_context_tool(agent=agent, messages=messages)
    parsed = json.loads(result)
    assert parsed["success"] is False
    assert parsed.get("aborted") is True
    assert parsed["message_count"] == 1


# ─── Handler — success path ──────────────────────────────────────────────────

def test_success_rewrites_messages_in_place():
    """When _compress_context returns a new shorter list, the tool copies it
    back into the caller's list so the loop continues on the compacted data."""
    agent = MagicMock()
    agent._cached_system_prompt = "sys"
    original = [
        {"role": "user", "content": "long verbose turn 1"},
        {"role": "assistant", "content": "long verbose reply 1"},
        {"role": "user", "content": "long verbose turn 2"},
        {"role": "assistant", "content": "long verbose reply 2"},
    ]
    compacted = [{"role": "user", "content": "summary"}]
    agent._compress_context.return_value = (compacted, "new_sys")

    # Pass a real list (the loop passes the live messages list).
    live_messages = list(original)
    result = compact_context_tool(
        agent=agent, messages=live_messages, focus_topic="the contract"
    )
    parsed = json.loads(result)
    assert parsed["success"] is True
    assert parsed["message_count_before"] == 4
    assert parsed["message_count_after"] == 1

    # The live list was rewritten in place.
    assert live_messages == compacted
    # Agent state updated.
    assert agent._cached_system_prompt == "new_sys"
    assert agent._session_messages is live_messages

    # focus_topic was forwarded.
    _args, kwargs = agent._compress_context.call_args
    assert kwargs.get("focus_topic") == "the contract"
    assert kwargs.get("force") is True


# ─── Handler — exception path ────────────────────────────────────────────────

def test_exception_returns_error_json():
    agent = MagicMock()
    agent._cached_system_prompt = "sys"
    agent._compress_context.side_effect = RuntimeError("summariser down")

    result = compact_context_tool(agent=agent, messages=[{"role": "user", "content": "x"}])
    parsed = json.loads(result)
    assert parsed["success"] is False
    assert "summariser down" in parsed["error"]
