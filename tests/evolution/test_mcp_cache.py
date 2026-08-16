# -*- coding: utf-8 -*-
"""Tests for MCP ttlMs / cacheScope result caching (Issue #2486, Slice B, SEP-2549)."""

from __future__ import annotations

import time
import pytest

from evolution.lib.mcp_cache import (
    CacheScope,
    CachedResult,
    MCPResultCache,
    get_global_mcp_cache,
)
from run_agent import AIAgent


class TestMCPResultCache:
    def test_hash_params(self):
        h1 = MCPResultCache.hash_params({"b": 2, "a": 1})
        h2 = MCPResultCache.hash_params({"a": 1, "b": 2})
        assert h1 == h2
        assert len(h1) == 64

    def test_set_and_get_session_scope(self):
        cache = MCPResultCache()
        params = {"query": "weather", "city": "Kyiv"}

        cache.set("weather_tool", params, {"temp": 22}, session_id="sess_1")

        # Hit in same session
        res = cache.get("weather_tool", params, session_id="sess_1")
        assert res == {"temp": 22}

        # Miss in different session
        res_other = cache.get("weather_tool", params, session_id="sess_2")
        assert res_other is None

    def test_set_and_get_global_scope(self):
        cache = MCPResultCache()
        params = {"resource": "schema"}

        cache.set(
            "schema_tool",
            params,
            {"types": ["A", "B"]},
            cache_scope=CacheScope.GLOBAL.value,
        )

        # Hit from any session
        res1 = cache.get("schema_tool", params, session_id="sess_alpha")
        res2 = cache.get("schema_tool", params, session_id="sess_beta")
        assert res1 == {"types": ["A", "B"]}
        assert res2 == {"types": ["A", "B"]}

    def test_ttl_expiration(self):
        cache = MCPResultCache()
        params = {"item": "clock"}

        entry = cache.set("time_tool", params, "12:00", ttl_ms=50, session_id="sess_t")
        assert cache.get("time_tool", params, session_id="sess_t") == "12:00"

        # Simulate time passing beyond ttlMs
        past_time = entry.created_at_ms + 100.0
        assert entry.is_expired(current_time_ms=past_time) is True

    def test_extract_cache_control_sep_2549(self):
        # Top-level directives
        payload1 = {"content": "data", "ttlMs": 3000, "cacheScope": "global"}
        ttl, scope = MCPResultCache.extract_cache_control(payload1)
        assert ttl == 3000
        assert scope == "global"

        # Nested metadata directives
        payload2 = {
            "result": 123,
            "_meta": {"ttl_ms": "5000", "cache_scope": "session"},
        }
        ttl2, scope2 = MCPResultCache.extract_cache_control(payload2)
        assert ttl2 == 5000
        assert scope2 == "session"

    def test_invalidate(self):
        cache = MCPResultCache()
        cache.set("tool_a", {"p": 1}, "res_a", session_id="s1")
        cache.set("tool_a", {"p": 2}, "res_a2", session_id="s1")
        cache.set("tool_b", {"p": 1}, "res_b", session_id="s1")
        cache.set("tool_a", {"p": 1}, "res_a_s2", session_id="s2")

        # Invalidate only tool_a in s1
        count = cache.invalidate(tool_name="tool_a", session_id="s1")
        assert count == 2
        assert cache.get("tool_a", {"p": 1}, session_id="s1") is None
        assert cache.get("tool_b", {"p": 1}, session_id="s1") == "res_b"
        assert cache.get("tool_a", {"p": 1}, session_id="s2") == "res_a_s2"

    def test_wrap_with_cache_decorator(self):
        cache = MCPResultCache()
        call_count = 0

        def handler(name: str, args: dict) -> str:
            nonlocal call_count
            call_count += 1
            return f"{name}_{args.get('val')}"

        cached_handler = cache.wrap_with_cache(handler, session_id="sess_wrap")
        res1 = cached_handler("lookup", {"val": 42})
        assert res1 == "lookup_42"
        assert call_count == 1

        # Second call with same params should hit cache
        res2 = cached_handler("lookup", {"val": 42})
        assert res2 == "lookup_42"
        assert call_count == 1


class TestAIAgentMCPCacheIntegration:
    def test_agent_cache_and_get_tool_result(self):
        agent = AIAgent(
            api_key="mock-key",
            base_url="http://localhost:8080/v1",
            model="test-model",
            quiet_mode=True,
            session_id="test_agent_mcp_sess",
        )

        params = {"resource_uri": "file:///docs/spec.md"}
        agent.cache_tool_result(
            "mcp_read_resource",
            params,
            {"body": "# Spec"},
            ttl_ms=60000,
            cache_scope="session",
        )

        res = agent.get_cached_tool_result("mcp_read_resource", params)
        assert res == {"body": "# Spec"}
