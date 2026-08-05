"""Tests for MCP get_prompt 'Unknown prompt' error enrichment (#1687).

When the tqmemory MCP server returns 'Unknown prompt: <name>' (because the
agent/cron asked for a name that is a skill, not a prompt — e.g.
'evolution-implementation', 'issue-lookup', 'semantic_search'), the
get_prompt handler must enrich the error JSON with:

  1. ``available_prompts`` — the real prompt names on the server, and
  2. ``hint`` — a recovery directive that suggests ``skill_view(name=...)``
     for skill-like names, breaking the retry spiral.

These tests exercise the production helper ``_enrich_missing_prompt_error``
directly (mirrors the approach in ``test_mcp_error_enrichment.py`` for the
tool handler), stubbing the MCP loop so no real asyncio/event-loop plumbing
is required.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest


def test_enrichment_lists_available_prompts(monkeypatch):
    """available_prompts must carry the real prompt names from the server."""
    from tools import mcp_tool

    server = MagicMock()
    server._rpc_lock = MagicMock()

    fake_prompts = [MagicMock(name="get_prompt"),
                    MagicMock(name="list_prompts")]
    for p, n in zip(fake_prompts, ["get_prompt", "list_prompts"]):
        p.name = n

    monkeypatch.setattr(mcp_tool, "_run_on_mcp_loop", lambda fn, timeout=30: fake_prompts)
    monkeypatch.setattr(mcp_tool, "_mark_server_call_started", lambda s: None)
    monkeypatch.setattr(mcp_tool, "_paginate_full_list",
                        lambda method, attr, sname: _async_result(fake_prompts))

    extra = mcp_tool._enrich_missing_prompt_error(
        server, "evolution-implementation", "tqmemory", 30.0
    )
    assert extra["available_prompts"] == ["get_prompt", "list_prompts"]


def test_enrichment_hint_for_skill_like_name():
    """A name with '-' must get the skill_view suggestion in the hint."""
    from tools import mcp_tool

    monkeypatch_holder = pytest.MonkeyPatch()
    monkeypatch_holder.setattr(mcp_tool, "_run_on_mcp_loop", lambda fn, timeout=30: [])
    monkeypatch_holder.setattr(mcp_tool, "_mark_server_call_started", lambda s: None)

    extra = mcp_tool._enrich_missing_prompt_error(
        MagicMock(), "issue-lookup", "tqmemory", 30.0
    )
    assert "skill_view(name='issue-lookup')" in extra["hint"]
    assert "skills, not prompts" in extra["hint"]
    monkeypatch_holder.undo()


def test_enrichment_hint_for_underscore_name():
    """A name with '_' must also get the skill_view suggestion."""
    from tools import mcp_tool

    mp = pytest.MonkeyPatch()
    mp.setattr(mcp_tool, "_run_on_mcp_loop", lambda fn, timeout=30: [])
    mp.setattr(mcp_tool, "_mark_server_call_started", lambda s: None)

    extra = mcp_tool._enrich_missing_prompt_error(
        MagicMock(), "semantic_search", "tqmemory", 30.0
    )
    assert "skill_view(name='semantic_search')" in extra["hint"]
    mp.undo()


def test_enrichment_hint_for_plain_name_no_skill_suggestion():
    """A name without '-' or '_' must NOT get the skill_view suggestion."""
    from tools import mcp_tool

    mp = pytest.MonkeyPatch()
    mp.setattr(mcp_tool, "_run_on_mcp_loop", lambda fn, timeout=30: [])
    mp.setattr(mcp_tool, "_mark_server_call_started", lambda s: None)

    extra = mcp_tool._enrich_missing_prompt_error(
        MagicMock(), "getprompt", "tqmemory", 30.0
    )
    assert "skill_view" not in extra["hint"]
    mp.undo()


def test_enrichment_degrades_gracefully_when_list_fails():
    """If the list_prompts call itself raises, available_prompts must be []
    and the hint must still be a usable string (no exception escapes)."""
    from tools import mcp_tool

    def boom(fn, timeout=30):
        raise RuntimeError("server gone")

    mp = pytest.MonkeyPatch()
    mp.setattr(mcp_tool, "_run_on_mcp_loop", boom)
    mp.setattr(mcp_tool, "_mark_server_call_started", lambda s: None)

    extra = mcp_tool._enrich_missing_prompt_error(
        MagicMock(), "evolution-implementation", "tqmemory", 30.0
    )
    assert extra["available_prompts"] == []
    assert "Use list_prompts" in extra["hint"]
    mp.undo()


def test_error_json_carries_enrichment_fields():
    """The full tool_error JSON for an unknown-prompt failure must contain
    both the original error text and the enrichment fields."""
    from tools.registry import tool_error

    base_msg = "MCP call failed: McpError: Unknown prompt: evolution-implementation"
    extra = {"available_prompts": ["get_prompt"], "hint": "use skill_view(...)"}
    payload = json.loads(tool_error(base_msg, **extra))
    assert payload["error"] == base_msg
    assert payload["available_prompts"] == ["get_prompt"]
    assert "skill_view" in payload["hint"]


class _async_result:
    """Tiny awaitable stand-in for _paginate_full_list in tests that patch it."""
    def __init__(self, value):
        self._value = value

    def __await__(self):
        yield
        return self._value
