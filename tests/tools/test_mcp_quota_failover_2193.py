"""Tests for MCP provider billing/quota failover (#2193).

When an MCP server's backing API returns HTTP 402 / Payment Required
(quota exhausted), the server is marked quota-exhausted for a 30 min
cooldown. Subsequent calls short-circuit with a diagnostic hint
directing the agent to alternative providers.

Key design constraint (#2193 rework): quota detection inspects
*transport-level exceptions* only — it must NOT inspect ``isError=True``
tool *results*, whose text is the tool's own error message (e.g. "file
not found" or a resource body that merely mentions "quota"). Conflating
the two was the bug that broke ``test_mcp_resource_content.py`` in the
first PR attempt.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch as mock_patch

import pytest

from tools import mcp_tool


# ---------------------------------------------------------------------------
# _is_quota_error — unit tests on the detector itself.
# ---------------------------------------------------------------------------


class TestIsQuotaError:
    def test_http_402_status_code(self):
        assert mcp_tool._is_quota_error(Exception("HTTP 402: Payment Required"))

    def test_payment_required_phrase(self):
        assert mcp_tool._is_quota_error(RuntimeError("Payment Required"))

    def test_quota_exhausted_phrase(self):
        assert mcp_tool._is_quota_error(RuntimeError("quota exhausted for key"))

    def test_quota_exhausted_hyphenated(self):
        assert mcp_tool._is_quota_error(RuntimeError("quota-exhausted"))

    def test_generic_error_not_quota(self):
        assert not mcp_tool._is_quota_error(Exception("connection refused"))

    def test_file_not_found_not_quota(self):
        assert not mcp_tool._is_quota_error(Exception("not_found: resource X"))

    def test_validation_error_not_quota(self):
        assert not mcp_tool._is_quota_error(Exception("validation failed"))

    def test_none_message(self):
        # An exception with an empty string repr must not crash.
        assert not mcp_tool._is_quota_error(Exception())


# ---------------------------------------------------------------------------
# Cooldown state helpers — _mark / _clear / _quota_cooldown_active.
# ---------------------------------------------------------------------------


class TestQuotaState:
    def setup_method(self):
        mcp_tool._server_quota_exhausted_until.clear()

    def teardown_method(self):
        mcp_tool._server_quota_exhausted_until.clear()

    def test_mark_sets_cooldown(self):
        mcp_tool._mark_quota_exhausted("srv")
        assert mcp_tool._quota_cooldown_active("srv")

    def test_clear_removes_cooldown(self):
        mcp_tool._mark_quota_exhausted("srv")
        mcp_tool._clear_quota_exhausted("srv")
        assert not mcp_tool._quota_cooldown_active("srv")

    def test_clear_when_not_set_is_noop(self):
        mcp_tool._clear_quota_exhausted("never-set")  # must not raise

    def test_cooldown_active_for_unset_server(self):
        assert not mcp_tool._quota_cooldown_active("never-set")

    def test_expired_cooldown_auto_clears(self):
        # Simulate an already-elapsed deadline.
        import time as _time

        mcp_tool._server_quota_exhausted_until["srv"] = _time.monotonic() - 1
        assert not mcp_tool._quota_cooldown_active("srv")
        # The stale entry must have been purged.
        assert "srv" not in mcp_tool._server_quota_exhausted_until


# ---------------------------------------------------------------------------
# Integration through _make_tool_handler — the real call paths.
# ---------------------------------------------------------------------------


def _make_handler_fixture(server_name="test-server", tool_name="my-tool"):
    """Build a handler + mock session for integration tests."""
    import asyncio

    fake_session = MagicMock()
    fake_server = SimpleNamespace(session=fake_session, _rpc_lock=None)

    def _fake_run_on_mcp_loop(coro_or_factory, timeout=30):
        coro = coro_or_factory() if callable(coro_or_factory) else coro_or_factory
        loop = asyncio.new_event_loop()
        try:

            async def _install_lock_and_run():
                for srv in list(mcp_tool._servers.values()):
                    if getattr(srv, "_rpc_lock", None) is None:
                        srv._rpc_lock = asyncio.Lock()
                return await coro

            return loop.run_until_complete(_install_lock_and_run())
        finally:
            loop.close()

    handler = mcp_tool._make_tool_handler(server_name, tool_name, 30.0)
    return handler, fake_session, fake_server, server_name, _fake_run_on_mcp_loop


@pytest.fixture()
def _quota_handler():
    """Yield a handler with a patched server + loop, quota state clean."""
    handler, fake_session, fake_server, server_name, loop_fn = _make_handler_fixture()
    mcp_tool._reset_server_error(server_name)
    mcp_tool._clear_quota_exhausted(server_name)
    try:
        with (
            mock_patch.dict(mcp_tool._servers, {server_name: fake_server}),
            mock_patch("tools.mcp_tool._run_on_mcp_loop", side_effect=loop_fn),
            mock_patch(
                "tools.mcp_tool._get_connected_server_for_call",
                return_value=fake_server,
            ),
        ):
            yield handler, fake_session, server_name
    finally:
        mcp_tool._reset_server_error(server_name)
        mcp_tool._clear_quota_exhausted(server_name)


class TestQuotaShortCircuit:
    def test_active_cooldown_short_circuits_with_hint(self, _quota_handler):
        handler, _session, server_name = _quota_handler
        mcp_tool._mark_quota_exhausted(server_name)

        data = json.loads(handler({}))
        assert "error" in data
        assert "quota-exhausted" in data["error"]
        assert "402" in data["error"]

    def test_short_circuit_does_not_call_tool(self, _quota_handler):
        handler, session, server_name = _quota_handler
        mcp_tool._mark_quota_exhausted(server_name)

        handler({})
        # The mock session's call_tool must never have been invoked.
        assert not session.call_tool.called


class TestQuotaDetectionOnException:
    def test_402_exception_marks_quota_and_returns_hint(self, _quota_handler):
        handler, session, server_name = _quota_handler
        session.call_tool = AsyncMock(
            side_effect=RuntimeError("HTTP 402: Payment Required")
        )

        data = json.loads(handler({}))
        assert "quota-exhausted" in data["error"]
        assert mcp_tool._quota_cooldown_active(server_name)

    def test_payment_required_exception_marks_quota(self, _quota_handler):
        handler, session, server_name = _quota_handler
        session.call_tool = AsyncMock(
            side_effect=Exception("Payment Required by upstream provider")
        )

        data = json.loads(handler({}))
        assert "quota-exhausted" in data["error"]
        assert mcp_tool._quota_cooldown_active(server_name)

    def test_generic_exception_does_not_mark_quota(self, _quota_handler):
        handler, session, server_name = _quota_handler
        session.call_tool = AsyncMock(side_effect=ConnectionError("connection refused"))

        data = json.loads(handler({}))
        # Generic exception surfaces the standard "MCP call failed" message.
        assert "MCP call failed" in data["error"]
        # Quota must NOT be active for a non-billing error.
        assert not mcp_tool._quota_cooldown_active(server_name)

    def test_second_call_after_402_short_circuits(self, _quota_handler):
        handler, session, server_name = _quota_handler
        session.call_tool = AsyncMock(
            side_effect=RuntimeError("HTTP 402: Payment Required")
        )

        # First call hits the exception path and marks quota.
        json.loads(handler({}))
        assert mcp_tool._quota_cooldown_active(server_name)

        # Second call short-circuits without touching call_tool again.
        call_count_before = session.call_tool.call_count
        json.loads(handler({}))
        assert session.call_tool.call_count == call_count_before


class TestQuotaClearOnSuccess:
    def test_success_clears_stale_quota_flag(self, _quota_handler):
        handler, session, server_name = _quota_handler
        # Simulate a quota flag whose cooldown has JUST elapsed (so the
        # short-circuit lets the call through as a probe). A successful
        # probe must then clear the flag entirely.
        import time as _time

        mcp_tool._server_quota_exhausted_until[server_name] = _time.monotonic() - 1
        session.call_tool = AsyncMock(
            return_value=SimpleNamespace(
                content=[SimpleNamespace(type="text", text="ok result")],
                isError=False,
                structuredContent=None,
            )
        )

        handler({})
        assert not mcp_tool._quota_cooldown_active(server_name)
        assert server_name not in mcp_tool._server_quota_exhausted_until


class TestIsErrorResultsNotTreatedAsQuota:
    """Regression: isError=True tool results must NOT trigger quota
    detection (#2193 rework — this was the bug that broke
    test_mcp_resource_content.py in the first PR attempt)."""

    def test_iserror_with_quota_word_not_marked(self, _quota_handler):
        handler, session, server_name = _quota_handler
        # A tool result whose resource text merely mentions "quota" —
        # this is the tool's own error, not a billing/transport failure.
        res = SimpleNamespace(
            uri="mem://err",
            mimeType="text/plain",
            text="quota exceeded for workspace W1",
            blob=None,
        )
        session.call_tool = AsyncMock(
            return_value=SimpleNamespace(
                content=[SimpleNamespace(type="resource", resource=res)],
                isError=True,
                structuredContent=None,
            )
        )

        data = json.loads(handler({}))
        # The tool's error text surfaces unchanged.
        assert "quota exceeded for workspace W1" in data["error"]
        # Quota failover must NOT have been triggered.
        assert not mcp_tool._quota_cooldown_active(server_name)

    def test_iserror_tool_failed_not_marked(self, _quota_handler):
        handler, session, server_name = _quota_handler
        session.call_tool = AsyncMock(
            return_value=SimpleNamespace(
                content=[SimpleNamespace(type="text", text="tool failed")],
                isError=True,
                structuredContent=None,
            )
        )

        data = json.loads(handler({}))
        assert "tool failed" in data["error"]
        assert not mcp_tool._quota_cooldown_active(server_name)

    def test_iserror_empty_falls_back_not_marked(self, _quota_handler):
        handler, session, server_name = _quota_handler
        session.call_tool = AsyncMock(
            return_value=SimpleNamespace(
                content=[],
                isError=True,
                structuredContent=None,
            )
        )

        data = json.loads(handler({}))
        assert data["error"] == "MCP tool returned an error"
        assert not mcp_tool._quota_cooldown_active(server_name)
