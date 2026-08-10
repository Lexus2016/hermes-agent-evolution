"""Tests for MCP provider billing/quota failover (issue #2193).

When an MCP server returns HTTP 402 / 'Payment Required' / 'quota exhausted',
the server is marked quota-exhausted with a 30min cooldown. Subsequent calls
short-circuit with a diagnostic hint directing the agent to alternative
providers (tavily, scrapling, web_search). Clears on successful call.
"""

import time
from unittest.mock import patch

import tools.mcp_tool as mcp_tool
from tools.mcp_tool import (
    _clear_quota_exhausted,
    _is_quota_error,
    _mark_quota_exhausted,
    _quota_cooldown_active,
    _quota_fallback_hint,
    _server_quota_exhausted_until,
)


class TestIsQuotaError:
    def test_402(self):
        assert _is_quota_error("HTTP 402: Payment Required") is True

    def test_payment_required(self):
        assert _is_quota_error("Payment Required") is True

    def test_quota_exhausted(self):
        assert _is_quota_error("quota exhausted") is True

    def test_subscription_required(self):
        assert _is_quota_error("subscription required for this model") is True

    def test_upgrade_for_access(self):
        assert _is_quota_error("upgrade for access") is True

    def test_normal_error_not_quota(self):
        assert _is_quota_error("connection refused") is False

    def test_empty_string(self):
        assert _is_quota_error("") is False

    def test_none(self):
        assert _is_quota_error("") is False  # empty/None treated same

    def test_case_insensitive(self):
        assert _is_quota_error("PAYMENT REQUIRED") is True
        assert _is_quota_error("Quota Exhausted") is True


class TestQuotaCooldown:
    def test_mark_and_check(self):
        _server_quota_exhausted_until.pop("test-srv", None)
        assert not _quota_cooldown_active("test-srv")
        _mark_quota_exhausted("test-srv")
        assert _quota_cooldown_active("test-srv")

    def test_clear(self):
        _mark_quota_exhausted("test-srv")
        assert _quota_cooldown_active("test-srv")
        _clear_quota_exhausted("test-srv")
        assert not _quota_cooldown_active("test-srv")

    def test_clear_when_not_set(self):
        _server_quota_exhausted_until.pop("test-srv", None)
        _clear_quota_exhausted("test-srv")  # should not raise

    def test_expiry(self):
        """Cooldown auto-clears after the TTL elapses."""
        _server_quota_exhausted_until["test-srv"] = time.monotonic() - 1
        assert not _quota_cooldown_active("test-srv")
        assert "test-srv" not in _server_quota_exhausted_until


class TestQuotaFallbackHint:
    def test_mentions_alternatives(self):
        _mark_quota_exhausted("jina")
        hint = _quota_fallback_hint("jina")
        assert "quota-exhausted" in hint
        assert "tavily" in hint
        assert "Do NOT retry" in hint

    def test_mentions_server_name(self):
        _mark_quota_exhausted("my-mcp-server")
        hint = _quota_fallback_hint("my-mcp-server")
        assert "my-mcp-server" in hint

    def teardown_method(self):
        _server_quota_exhausted_until.clear()
