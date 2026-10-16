"""Stable lock facade and reentrant continuation after send-method extraction."""
import asyncio
from types import SimpleNamespace

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.telegram.adapter import TelegramAdapter


@pytest.mark.asyncio
async def test_raw_lock_identity_and_reentrant_guard_preserve_ownership():
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="synthetic-token"))
    lock = adapter._chat_send_lock("-100777")
    assert lock is adapter._chat_send_lock(-100777)
    assert not lock.locked()
    async with asyncio.timeout(2):
        async with adapter._chat_send_guard("-100777"):
            assert lock.locked()
            async with adapter._chat_send_guard(-100777):
                assert lock.locked()
            assert lock.locked()
    assert not lock.locked()


@pytest.mark.asyncio
async def test_transport_failure_diagnostics_do_not_log_unredacted_exception(monkeypatch, caplog):
    from plugins.platforms.telegram import adapter as module

    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="synthetic-token"))
    adapter._rich_messages_enabled = False
    async def broken(**kwargs):
        raise RuntimeError("synthetic-private-transport-secret")
    adapter._bot = SimpleNamespace(send_message=broken)
    monkeypatch.setattr(module, "_redact_telegram_error_text", lambda error: "redacted transport failure")
    result = await adapter.send("-100777", "hello")
    assert not result.success
    assert result.error == "redacted transport failure"
    assert "synthetic-private-transport-secret" not in caplog.text
    assert "redacted transport failure" in caplog.text
    assert not adapter._chat_send_lock("-100777").locked()
