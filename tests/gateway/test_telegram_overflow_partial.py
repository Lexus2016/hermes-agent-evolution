"""Regression coverage for partial Telegram overflow delivery."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.base import SendResult
from plugins.platforms.telegram.adapter import TelegramAdapter
from gateway.stream_consumer import GatewayStreamConsumer


def _message(message_id: int | str) -> SimpleNamespace:
    return SimpleNamespace(message_id=message_id)


@pytest.fixture
def telegram_adapter() -> TelegramAdapter:
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="fake-token"))
    adapter._bot = MagicMock()
    object.__setattr__(adapter, "MAX_MESSAGE_LENGTH", 160)
    return adapter


@pytest.mark.asyncio
async def test_edit_overflow_split_reports_later_partial_failure_after_some_continuations_land(telegram_adapter):
    """Partial metadata tracks the last delivered continuation before failure."""
    content = "word " * 120
    telegram_adapter._bot.edit_message_text = AsyncMock(return_value=True)
    telegram_adapter._bot.send_message = AsyncMock(
        side_effect=[
            _message(202),
            RuntimeError("telegram send failed"),
            RuntimeError("telegram send failed"),
        ]
    )

    result = await telegram_adapter._edit_overflow_split(
        "12345", "201", content, finalize=False, metadata={"thread_id": "77"}
    )

    assert result.success is False
    assert result.message_id == "202"
    assert result.raw_response["partial_overflow"] is True
    assert result.raw_response["delivered_chunks"] == 2
    assert result.raw_response["last_message_id"] == "202"
    assert result.continuation_message_ids == ("202",)




# ── send() partial split delivery ───────────────────────────────────────────────
# A payload over MAX_MESSAGE_LENGTH goes out as several messages, so send() is not atomic:
# a refusal on a later chunk (in production, a flood-control fail-closed) leaves the earlier
# chunks on screen. Reported as a bare failure, the caller either drops the tail — the reader
# gets a reply that stops mid-sentence — or re-sends the whole payload and duplicates the
# visible head. send() must report the partial the same way _edit_overflow_split does.

_SPLIT_BODY = "\n".join(f"line {i} with a few more words here" for i in range(1, 41))


class _FakeClock:
    """Real ``time`` module, except ``monotonic()`` only moves when a faked sleep says so."""

    def __init__(self, start: float = 1_000.0) -> None:
        self._now = start

    def advance(self, seconds: float) -> None:
        self._now += float(seconds)

    def monotonic(self) -> float:
        return self._now

    def __getattr__(self, name):  # every other time.* stays real
        import time as _real_time
        return getattr(_real_time, name)


def _flood_on_call(n: int, wait: float = 6.0):
    """send_message side effect: every call succeeds except the ``n``-th, which raises an
    over-the-inline-cap Telegram flood refusal."""
    state = {"calls": 0}

    def _side_effect(*_args, **kwargs):
        state["calls"] += 1
        if state["calls"] == n:
            err = RuntimeError("Flood control exceeded. Retry in 6 seconds")
            err.retry_after = wait
            raise err
        return _message(1000 + state["calls"])

    return _side_effect, state


@pytest.mark.asyncio
async def test_send_reports_partial_when_a_later_chunk_is_refused(telegram_adapter):
    """The head chunks are on screen: say so, and name the undelivered tail."""
    telegram_adapter._bot.send_message = AsyncMock(side_effect=_flood_on_call(2)[0])

    result = await telegram_adapter.send("12345", _SPLIT_BODY)

    assert result.success is False
    raw = result.raw_response
    assert raw["partial_overflow"] is True
    assert raw["delivered_chunks"] == 1
    assert raw["total_chunks"] > 1
    assert raw["last_message_id"] == "1001"
    assert result.message_id == "1001"  # not None: something IS on screen
    # The reported halves account for the whole payload, so a resume loses nothing.
    assert raw["delivered_prefix"] + raw["undelivered_tail"] == _SPLIT_BODY
    assert result.retry_after == 6.0


@pytest.mark.asyncio
async def test_send_without_a_refusal_reports_no_partial(telegram_adapter):
    """A fully delivered split stays an ordinary success."""
    telegram_adapter._bot.send_message = AsyncMock(side_effect=_flood_on_call(0)[0])

    result = await telegram_adapter.send("12345", _SPLIT_BODY)

    assert result.success is True
    assert "partial_overflow" not in (result.raw_response or {})


@pytest.mark.asyncio
async def test_send_retry_resends_only_the_undelivered_tail(telegram_adapter, monkeypatch):
    """_send_with_retry must not re-send a head the platform already accepted."""
    import gateway.platforms.base as base
    import plugins.platforms.telegram.adapter as tg

    # Faking the retry sleep means faking the clock with it: the refusal arms a per-chat flood
    # cooldown for exactly ``wait`` seconds, and _send_with_retry sleeps ``wait`` + jitter, so in
    # production the retry lands after the cooldown. A mocked sleep that leaves time frozen would
    # have the retry gated by the penalty it just waited out.
    clock = _FakeClock()
    monkeypatch.setattr(tg, "time", clock)

    async def _sleep(seconds, *_args, **_kwargs):
        clock.advance(seconds)

    monkeypatch.setattr(base.asyncio, "sleep", _sleep)
    side_effect, state = _flood_on_call(2)
    sent: list[str] = []

    def _record(*_args, **kwargs):
        result = side_effect(*_args, **kwargs)
        sent.append(kwargs.get("text", ""))
        return result

    telegram_adapter._bot.send_message = AsyncMock(side_effect=_record)

    result = await telegram_adapter._send_with_retry(chat_id="12345", content=_SPLIT_BODY)

    assert result.success is True
    # Every source line lands exactly once — no gap, and no duplicated head.
    delivered = " ".join(sent)
    for line in _SPLIT_BODY.splitlines():
        assert delivered.count(line) == 1, f"{line!r} delivered {delivered.count(line)}x"


def test_undelivered_tail_after_partial_ignores_a_whole_message_failure():
    """Only a reported partial changes what gets resent."""
    from gateway.platforms.base import undelivered_tail_after_partial

    assert undelivered_tail_after_partial(
        SendResult(success=False, error="boom"), "abc") is None
    assert undelivered_tail_after_partial(
        SendResult(success=False, error="flood", raw_response={"partial_overflow": True,
                                                              "undelivered_tail": "c"}), "abc") == "c"
    # No explicit tail: derive it from the delivered prefix.
    assert undelivered_tail_after_partial(
        SendResult(success=False, error="flood", raw_response={"partial_overflow": True,
                                                              "delivered_prefix": "ab"}), "abc") == "c"
