"""Regression coverage for per-chat Telegram send ordering.

Nothing above the adapter serializes by CHAT — gateway/turn_lease.py serializes per SESSION — so two
sources aimed at one chat (a cron report and a DM reply, a notification landing mid-turn) used to
interleave their chunks. Two concurrent 3-chunk sends measured as a perfect A-B-A-B-A-B alternation:
the reader saw part 1, an unrelated message, then part 2, i.e. a reply that stops mid-sentence.
"""

import asyncio
from types import SimpleNamespace

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.telegram.adapter import TelegramAdapter

_REPORT = "\n".join("REPORT " + "detail " * 12 for _ in range(120))   # ~3 chunks
_ALERT = "\n".join("ALERT " + "notice " * 12 for _ in range(120))     # ~3 chunks


class _OrderRecordingBot:
    """Records the tag of every delivered chunk. The await is the interleaving point a real
    transport has."""

    def __init__(self, flood_on: int | None = None) -> None:
        self.order: list[str] = []
        self.calls = 0
        self._flood_on = flood_on

    async def send_message(self, text=None, **_kwargs):
        self.calls += 1
        tag = (text or "").strip().split()[0]
        await asyncio.sleep(0)  # yield, as an HTTP round-trip would
        if self.calls == self._flood_on:
            err = RuntimeError("Flood control exceeded. Retry in 41 seconds")
            err.retry_after = 41.0
            raise err
        self.order.append(tag)
        return SimpleNamespace(message_id=1000 + self.calls)


def _adapter(flood_on: int | None = None) -> TelegramAdapter:
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="fake-token"))
    adapter._bot = _OrderRecordingBot(flood_on)
    adapter._rich_messages_enabled = False
    return adapter


def _source_switches(order: list[str]) -> int:
    return sum(1 for i in range(1, len(order)) if order[i] != order[i - 1])


@pytest.mark.asyncio
async def test_concurrent_sends_to_one_chat_stay_contiguous():
    """Each message's chunks land as one block, so neither reply is broken by the other."""
    adapter = _adapter()

    await asyncio.gather(
        adapter.send("-100777", _REPORT), adapter.send("-100777", _ALERT))

    order = adapter._bot.order
    assert len(order) > 2, "payloads must actually split for this to mean anything"
    # Exactly one boundary: everything from one sender, then everything from the other.
    assert _source_switches(order) == 1, f"interleaved: {order}"


@pytest.mark.asyncio
async def test_different_chats_are_not_serialized_behind_each_other():
    """The lock is per chat: an unrelated conversation must not wait."""
    adapter = _adapter()
    lock_a = adapter._chat_send_lock("-100777")
    lock_b = adapter._chat_send_lock("-100888")

    assert lock_a is not lock_b

    await asyncio.gather(
        adapter.send("-100777", _REPORT), adapter.send("-100888", _ALERT))

    assert adapter._bot.calls == len(adapter._bot.order)


@pytest.mark.asyncio
async def test_queued_sends_do_not_fire_into_a_refusal_the_first_one_hit():
    """A burst that all cleared the gate before queueing re-checks it once the queue moves.

    Production logged 7-14 refusals per second inside one penalty window; each was a round-trip
    that could only be rejected, and rejections lengthen the penalty.
    """
    adapter = _adapter(flood_on=1)

    results = await asyncio.gather(
        adapter.send("-100777", _REPORT), adapter.send("-100777", _ALERT))

    assert [r.success for r in results] == [False, False]
    # One request total: the refusal, then nothing. Unqueued, this was ~6.
    assert adapter._bot.calls == 1


@pytest.mark.asyncio
async def test_lock_is_released_when_a_send_raises():
    """A transport blowing up must not wedge the chat forever."""
    adapter = _adapter()

    async def _boom(**_kwargs):
        raise RuntimeError("transport exploded")

    adapter._bot.send_message = _boom
    result = await adapter.send("-100777", "short message")

    assert result.success is False
    assert not adapter._chat_send_lock("-100777").locked()


@pytest.mark.asyncio
async def test_idle_locks_are_pruned():
    """One entry per chat ever messaged must not accumulate for the process's lifetime."""
    from plugins.platforms.telegram.adapter import _CHAT_SEND_LOCK_PRUNE_AT

    adapter = _adapter()
    for i in range(_CHAT_SEND_LOCK_PRUNE_AT + 1):
        adapter._chat_send_lock(f"chat-{i}")

    assert len(adapter._chat_send_locks) <= _CHAT_SEND_LOCK_PRUNE_AT
