"""Outbound pacing: stop earning Telegram flood penalties instead of recovering from them.

Three delivery defects in this adapter had the same origin — sending faster than Telegram allows,
collecting a 9-269 second penalty, and then reconstructing what the reader missed. Reconstruction is
where replies lost their tail, interleaved, or arrived split mid-word.
"""

import asyncio
import time

import pytest

from plugins.platforms.telegram.send_pacer import TelegramSendPacer


@pytest.mark.asyncio
async def test_consecutive_sends_to_one_chat_are_spaced():
    pacer = TelegramSendPacer(per_chat_interval=0.2, global_rate=1000.0)

    started = time.monotonic()
    for _ in range(4):
        await pacer.wait_turn("chat")
    elapsed = time.monotonic() - started

    # Three gaps between four sends; the first goes immediately.
    assert elapsed == pytest.approx(0.6, abs=0.15)


@pytest.mark.asyncio
async def test_other_chats_are_not_delayed():
    """The per-chat limit is per chat: an unrelated conversation must not queue behind one."""
    pacer = TelegramSendPacer(per_chat_interval=5.0, global_rate=1000.0)

    started = time.monotonic()
    await asyncio.gather(*(pacer.wait_turn(f"chat-{i}") for i in range(5)))

    assert time.monotonic() - started < 0.2


@pytest.mark.asyncio
async def test_concurrent_sends_to_one_chat_queue_rather_than_burst():
    """Waiters must not all wake on the same deadline — that would re-create the burst."""
    pacer = TelegramSendPacer(per_chat_interval=0.2, global_rate=1000.0)

    started = time.monotonic()
    await asyncio.gather(*(pacer.wait_turn("chat") for _ in range(4)))

    assert time.monotonic() - started == pytest.approx(0.6, abs=0.15)


@pytest.mark.asyncio
async def test_the_global_budget_bursts_then_paces():
    """Across chats a short burst is allowed, then the rate holds."""
    pacer = TelegramSendPacer(per_chat_interval=0.0, global_rate=10.0, global_burst=3)

    started = time.monotonic()
    await asyncio.gather(*(pacer.wait_turn(f"chat-{i}") for i in range(8)))
    elapsed = time.monotonic() - started

    # Three go at once; the remaining five arrive at ten per second.
    assert elapsed == pytest.approx(0.5, abs=0.2)


@pytest.mark.asyncio
async def test_pacing_can_be_switched_off():
    pacer = TelegramSendPacer(per_chat_interval=0.0, global_rate=30.0, global_burst=30)

    assert await pacer.wait_turn("chat") == 0.0
    assert await pacer.wait_turn("chat") == 0.0


@pytest.mark.asyncio
async def test_idle_chats_are_pruned():
    """One entry per chat ever messaged must not accumulate for the process's lifetime."""
    from plugins.platforms.telegram.send_pacer import _PRUNE_AT, _STALE_AFTER_SECONDS

    pacer = TelegramSendPacer(per_chat_interval=0.0, global_rate=1000.0)
    stale = time.monotonic() - _STALE_AFTER_SECONDS - 1
    pacer._next_allowed = {f"old-{i}": stale for i in range(_PRUNE_AT)}

    await pacer.wait_turn("fresh")

    assert len(pacer._next_allowed) < _PRUNE_AT


# ── the adapter charges one slot per message that actually goes out ─────────────

@pytest.mark.asyncio
async def test_a_rich_attempt_that_falls_back_is_paced_once():
    """A rich send that degrades to legacy is still ONE message, so it costs one slot.

    Pacing sits before the rich fast-path and before each legacy chunk; without a guard, a rich
    attempt that returns None (capability error, DM-topic skip) charged the message twice — a
    second of latency and a global token spent on a send that never happened.
    """
    import time
    from types import SimpleNamespace

    from gateway.config import PlatformConfig
    from plugins.platforms.telegram.adapter import TelegramAdapter

    class _Bot:
        def __init__(self):
            self.sent = 0

        async def send_message(self, text=None, **_kwargs):
            self.sent += 1
            return SimpleNamespace(message_id=self.sent)

        async def do_api_request(self, *_a, **_k):
            raise Exception("Method not found: sendRichMessage")

    adapter = TelegramAdapter(
        PlatformConfig(enabled=True, token="t", extra={"rich_messages": True}))
    adapter._bot = _Bot()
    rich_eligible = "| a | b |\n|---|---|\n| 1 | 2 |"

    started = time.monotonic()
    assert (await adapter.send("-100777", rich_eligible)).success
    first = time.monotonic() - started

    started = time.monotonic()
    assert (await adapter.send("-100777", rich_eligible)).success
    second = time.monotonic() - started

    assert first < 0.3, "the first message must not wait — nothing preceded it"
    # The second is held by the per-chat interval, proving exactly one slot was charged for the first.
    assert second == pytest.approx(adapter._send_pacer.per_chat_interval, abs=0.25)


# ── edits share the budget with sends ───────────────────────────────────────────
# Telegram counts an edit against the same per-chat allowance as a send. Keeping two independent
# throttles (0.8s for streaming edits, ~1s for sends) let one chat receive more than two messages a
# second, which is what kept earning the penalties that broke replies apart.

def test_an_edit_over_budget_is_skipped_not_delayed():
    """A preview edit shows text the next tick shows anyway: skipping costs nothing, waiting does."""
    pacer = TelegramSendPacer(per_chat_interval=1.0, global_rate=1000.0)

    allowed = [pacer.try_slot("chat") for _ in range(5)]

    assert allowed.count(True) == 1
    assert allowed.count(False) == 4


@pytest.mark.asyncio
async def test_a_send_and_an_edit_draw_on_one_budget():
    pacer = TelegramSendPacer(per_chat_interval=1.0, global_rate=1000.0)

    await pacer.wait_turn("chat")

    assert pacer.try_slot("chat") is False, "an edit must not slip past a send's slot"
    assert pacer.try_slot("other") is True, "the budget is per chat"


def test_the_slot_frees_after_the_interval():
    pacer = TelegramSendPacer(per_chat_interval=0.05, global_rate=1000.0)

    assert pacer.try_slot("chat") is True
    assert pacer.try_slot("chat") is False
    time.sleep(0.06)
    assert pacer.try_slot("chat") is True


def test_adapters_without_a_budget_never_gate_edits():
    """The consumer asks every adapter; the base answer must not throttle anyone."""
    from gateway.platforms.base import BasePlatformAdapter

    assert BasePlatformAdapter.reserve_stream_edit_slot(object(), "chat") is True
