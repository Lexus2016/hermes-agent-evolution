"""Tests for browser_navigate retry + web_extract fallback (issue #213)."""

import json
from unittest.mock import patch, MagicMock

import pytest

import tools.browser_tool as browser_tool


def _reset_state(monkeypatch):
    """Clear module caches and mutable state so tests start clean."""
    monkeypatch.setattr(browser_tool, "_active_sessions", {})
    monkeypatch.setattr(browser_tool, "_last_active_session_key", {})
    monkeypatch.setattr(browser_tool, "_cached_cloud_provider", None)
    monkeypatch.setattr(browser_tool, "_cloud_provider_resolved", False)
    monkeypatch.setattr(browser_tool, "_start_browser_cleanup_thread", lambda: None)
    monkeypatch.setattr(browser_tool, "_update_session_activity", lambda t: None)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    _reset_state(monkeypatch)
    yield


class TestBrowserNavigateRetryAndFallback:

    def test_navigate_success_no_retries(self, monkeypatch):
        """Happy path succeeds on the first attempt."""
        calls = []

        def fake_run(session_key, command, args, timeout=None, _engine_override=None):
            calls.append((command, args))
            if command == "open":
                return {"success": True, "data": {"url": args[0], "title": "Example"}}
            if command == "snapshot":
                return {"success": True, "data": {"snapshot": "- heading \"Hi\" [e1]", "refs": {"e1": "h1"}}}
            return {"success": False, "error": "unexpected"}

        monkeypatch.setattr(browser_tool, "_run_browser_command", fake_run)
        monkeypatch.setattr(browser_tool, "_get_command_timeout", lambda: 30)
        monkeypatch.setattr(browser_tool, "_is_local_backend", lambda: True)
        monkeypatch.setattr(browser_tool, "_is_camofox_mode", lambda: False)

        result = json.loads(browser_tool.browser_navigate("https://example.com", task_id="t1"))

        assert result["success"] is True
        assert result["url"] == "https://example.com"
        assert result["title"] == "Example"
        assert result["retries"] == 0
        assert "navigate_warning" not in result
        assert len(calls) == 2  # open + snapshot

    def test_navigate_succeeds_after_retry(self, monkeypatch):
        """First open fails, retry succeeds."""
        attempts = []

        def fake_run(session_key, command, args, timeout=None, _engine_override=None):
            attempts.append((command, args))
            if command == "open":
                if len([c for c in attempts if c[0] == "open"]) == 1:
                    return {"success": False, "error": "transient timeout"}
                return {"success": True, "data": {"url": args[0], "title": "Example"}}
            if command == "snapshot":
                return {"success": True, "data": {"snapshot": "", "refs": {}}}
            return {"success": False, "error": "unexpected"}

        monkeypatch.setattr(browser_tool, "_run_browser_command", fake_run)
        monkeypatch.setattr(browser_tool, "_get_command_timeout", lambda: 30)
        monkeypatch.setattr(browser_tool, "_is_local_backend", lambda: True)
        monkeypatch.setattr(browser_tool, "_is_camofox_mode", lambda: False)
        monkeypatch.setattr(browser_tool.time, "sleep", lambda s: None)

        result = json.loads(browser_tool.browser_navigate("https://example.com", task_id="t2"))

        assert result["success"] is True
        assert result["retries"] == 1
        assert "transient timeout" in result["navigate_warning"]
        assert len([c for c in attempts if c[0] == "open"]) == 2

    def test_navigate_exhausted_then_fallback_to_web_extract(self, monkeypatch):
        """All retries fail and web_extract returns content."""

        def fake_run(session_key, command, args, timeout=None, _engine_override=None):
            if command == "open":
                return {"success": False, "error": "browser unavailable"}
            return {"success": False, "error": "unexpected"}

        async def fake_web_extract(urls, format=None, use_llm_processing=True, model=None, min_length=5000):
            return json.dumps({
                "success": True,
                "results": [{"url": urls[0], "title": "Extracted", "content": "extracted text"}],
            })

        monkeypatch.setattr(browser_tool, "_run_browser_command", fake_run)
        monkeypatch.setattr(browser_tool, "_get_command_timeout", lambda: 30)
        monkeypatch.setattr(browser_tool, "_is_local_backend", lambda: True)
        monkeypatch.setattr(browser_tool, "_is_camofox_mode", lambda: False)
        monkeypatch.setattr(browser_tool.time, "sleep", lambda s: None)

        with patch.object(browser_tool.web_tools, "web_extract_tool", fake_web_extract):
            result = json.loads(browser_tool.browser_navigate("https://example.com", task_id="t3"))

        assert result["success"] is True
        assert result["retries"] == 3
        assert result["fallback_used"] is True
        assert result["fallback"] == "web_extract"
        assert result["source"] == "web_extract"
        assert result["content"] == "extracted text"
        assert result["original_error"] == "browser unavailable"
        assert "error" not in result or result["error"] is None

    def test_navigate_exhausted_and_fallback_fails(self, monkeypatch):
        """Retries and web_extract both fail; diagnostic is clear."""

        def fake_run(session_key, command, args, timeout=None, _engine_override=None):
            if command == "open":
                return {"success": False, "error": "browser crashed"}
            return {"success": False, "error": "unexpected"}

        async def fake_web_extract(urls, format=None, use_llm_processing=True, model=None, min_length=5000):
            return json.dumps({"success": False, "error": "web_extract quota exceeded"})

        monkeypatch.setattr(browser_tool, "_run_browser_command", fake_run)
        monkeypatch.setattr(browser_tool, "_get_command_timeout", lambda: 30)
        monkeypatch.setattr(browser_tool, "_is_local_backend", lambda: True)
        monkeypatch.setattr(browser_tool, "_is_camofox_mode", lambda: False)
        monkeypatch.setattr(browser_tool.time, "sleep", lambda s: None)

        with patch.object(browser_tool.web_tools, "web_extract_tool", fake_web_extract):
            result = json.loads(browser_tool.browser_navigate("https://example.com", task_id="t4"))

        assert result["success"] is False
        assert result["retries"] == 3
        assert result["fallback_used"] is True
        assert result["original_error"] == "browser crashed"
        assert result["fallback_error"] == "web_extract quota exceeded"
        assert "web_extract" in result["fallback"]

    def test_navigate_success_unchanged_when_no_failure(self, monkeypatch):
        """Successful navigation still returns snapshot and does not add warning."""

        def fake_run(session_key, command, args, timeout=None, _engine_override=None):
            if command == "open":
                return {"success": True, "data": {"url": args[0], "title": "OK"}}
            if command == "snapshot":
                return {"success": True, "data": {"snapshot": "- link [e1]", "refs": {"e1": "a"}}}
            return {"success": False, "error": "unexpected"}

        monkeypatch.setattr(browser_tool, "_run_browser_command", fake_run)
        monkeypatch.setattr(browser_tool, "_get_command_timeout", lambda: 30)
        monkeypatch.setattr(browser_tool, "_is_local_backend", lambda: True)
        monkeypatch.setattr(browser_tool, "_is_camofox_mode", lambda: False)

        result = json.loads(browser_tool.browser_navigate("https://example.com", task_id="t5"))

        assert result["success"] is True
        assert result["retries"] == 0
        assert "navigate_warning" not in result
        assert "[e1]" in result["snapshot"]
        assert result["element_count"] == 1
