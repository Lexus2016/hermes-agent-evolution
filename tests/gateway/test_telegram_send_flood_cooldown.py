"""Regression coverage for the Telegram per-chat send flood cooldown.

Telegram lengthens a flood penalty while a bot keeps hammering it, and production did exactly
that: of 1,253 flood refusals over four days, most arrived in bursts of 7-14 per second inside ONE
penalty window. Every one was a round-trip that could only be rejected — and that also fed the ban.
A refusal must therefore arm a local gate, so the next send to that chat fails closed without an
API call while still returning the typed result the delivery ledger owns redelivery from.
"""

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from gateway.delivery_ledger import flood_wait_seconds, is_flood_error
from plugins.platforms.telegram.adapter import _SEND_COOLDOWN_CAP_SECONDS, TelegramAdapter


def _flood_error(wait: float = 41.0) -> RuntimeError:
    err = RuntimeError(f"Flood control exceeded. Retry in {int(wait)} seconds")
    err.retry_after = wait
    return err


@pytest.fixture
def adapter() -> TelegramAdapter:
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="fake-token"))
    adapter._bot = MagicMock()
    adapter._rich_messages_enabled = False
    return adapter


@pytest.mark.asyncio
async def test_flood_refusal_gates_the_next_send_to_that_chat(adapter):
    """No second request into an active penalty — and the result stays ledger-readable."""
    adapter._bot.send_message = AsyncMock(side_effect=_flood_error())

    first = await adapter.send("-100777", "first")
    calls_after_first = adapter._bot.send_message.await_count
    second = await adapter.send("-100777", "second")

    assert first.success is False
    assert second.success is False
    # The gate short-circuits: the second send never reached the transport.
    assert adapter._bot.send_message.await_count == calls_after_first
    # ...and the ledger still sees a flood refusal with a readable wait, so it owns redelivery.
    assert is_flood_error(second.error)
    assert flood_wait_seconds(second.error) == pytest.approx(41.0, abs=1.0)
    assert second.retry_after == pytest.approx(41.0, abs=1.0)


@pytest.mark.asyncio
async def test_cooldown_is_scoped_to_the_refused_chat(adapter):
    """A penalty on one chat must not silence every other conversation."""
    adapter._bot.send_message = AsyncMock(
        side_effect=[_flood_error(), SimpleNamespace(message_id=7)])

    await adapter.send("-100777", "refused")
    other = await adapter.send("-100888", "unrelated chat")

    assert other.success is True


@pytest.mark.asyncio
async def test_expired_cooldown_lets_the_next_send_through(adapter):
    """The gate is a wait, not a latch."""
    adapter._bot.send_message = AsyncMock(
        side_effect=[_flood_error(), SimpleNamespace(message_id=9)])

    await adapter.send("-100777", "refused")
    adapter._telegram_send_cooldown_until["-100777"] = time.monotonic() - 0.01
    retried = await adapter.send("-100777", "after the penalty")

    assert retried.success is True
    # A delivered send proves the penalty is over, so nothing stays armed.
    assert "-100777" not in adapter._telegram_send_cooldown_until


def test_absurd_retry_after_is_capped(adapter):
    """A bogus or huge wait must not park a chat for hours; the ledger row's deadline governs."""
    adapter._record_send_cooldown("-100999", 99_999.0)

    assert adapter._send_cooldown_remaining("-100999") <= _SEND_COOLDOWN_CAP_SECONDS


def test_unparsable_wait_does_not_arm_the_gate(adapter):
    """Garbage in, no gate — never silence a chat on a value we could not read."""
    adapter._record_send_cooldown("-100999", "not-a-number")

    assert adapter._send_cooldown_remaining("-100999") == 0.0
