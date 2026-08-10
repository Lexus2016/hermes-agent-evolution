"""Tests for MCP quota/billing-exhaustion failover (issue #2193).

When an MCP server's backing API returns HTTP 402 / "Payment Required" /
"quota exhausted", the server is marked quota-exhausted for a 30-minute
cooldown. Subsequent calls short-circuit with a diagnostic hint.
"""

import json
import time
import pytest

import tools.mcp_tool as mcp_tool
from tools.mcp_tool import (
    _clear_quota_exhausted,
    _is_quota_error,
    _mark_quota_exhausted,
    _quota_cooldown_active,
)


@pytest.fixture(autouse=True)
def _clean_quota_state():
    mcp_tool._server_quota_exhausted_until.clear()
    yield
    mcp_tool._server_quota_exhausted_until.clear()


class TestIsQuotaError:
    def test_detects_quota_keywords(self):
        for msg in (
            "HTTP 402 Payment Required",
            "Payment Required",
            "quota exceeded",
            "billing limit reached",
            "QUOTA EXHAUSTED",
            "HTTP 402",
        ):
            assert _is_quota_error(Exception(msg)), msg

    def test_rejects_non_quota(self):
        for exc in (
            Exception("connection refused"),
            TimeoutError("timed out"),
            Exception(""),
        ):
            assert not _is_quota_error(exc)


class TestQuotaState:
    def test_mark_clear_expire(self):
        _mark_quota_exhausted("srv")
        assert _quota_cooldown_active("srv")
        _clear_quota_exhausted("srv")
        assert not _quota_cooldown_active("srv")
        assert not _quota_cooldown_active("unknown")
        # Expired entry auto-cleans.
        _mark_quota_exhausted("srv")
        mcp_tool._server_quota_exhausted_until["srv"] = time.monotonic() - 1
        assert not _quota_cooldown_active("srv")
        assert "srv" not in mcp_tool._server_quota_exhausted_until


class TestHandlerShortCircuit:
    def test_returns_quota_hint(self):
        handler = mcp_tool._make_tool_handler("test-srv", "tool1", 30.0)
        _mark_quota_exhausted("test-srv")
        result = json.loads(handler({}))
        assert "error" in result
        assert "quota-exhausted" in result["error"]
        assert "test-srv" in result["error"]
        assert "alternative providers" in result["error"]


class TestIsErrorPreserved:
    """isError path is untouched — prior PR #2218 broke it by intercepting all."""

    def test_non_billing_text_not_matched(self):
        msg = "tool failed"
        assert not any(
            kw in msg.lower() for kw in ("402", "payment required", "quota", "billing")
        )
        assert not _quota_cooldown_active("any-server")
