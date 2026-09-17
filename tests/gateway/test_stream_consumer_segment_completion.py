"""A segment break must not seal a message whose own deltas are still arriving.

The agent decides a segment is over from the COMPLETED assistant message, while that message's
deltas may still be in flight, and its "already streamed" test is a PREFIX match
(agent/stream_delivery.py:108) — true even when only a few characters have landed. Sealing then
published a fragment as a finished message and pushed the remainder into the next one, so a single
sentence arrived split mid-word: observed in production as 'Прав' + 'лю точними живими рядками…'.
"""

import asyncio
from types import SimpleNamespace

import pytest

from gateway.stream_consumer import GatewayStreamConsumer

_SENTENCE = "Правлю точними живими рядками (мій попередній EN-паттерн не збігався):"
_ROUND_1 = "Граматичний баг у спільному L5 — правлю лише в polish-шарі."
_ROUND_2 = "Тепер оновлюю 3 застарілі тести."
_UNRELATED = "Це зовсім інше повідомлення, не продовження."


class _EditAdapter:
    """Edit-transport double: send() opens a message, edit_message() updates it in place."""

    MAX_MESSAGE_LENGTH = 4096
    splits_long_messages = True

    def __init__(self) -> None:
        self.msgs: dict[str, str] = {}
        self._n = 0

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self._n += 1
        mid = f"m{self._n}"
        self.msgs[mid] = content
        return SimpleNamespace(success=True, message_id=mid)

    async def edit_message(self, chat_id, message_id, content, *, finalize=False, metadata=None):
        self.msgs[message_id] = content
        return SimpleNamespace(success=True, message_id=message_id)

    def message_len_fn_for_chat(self, chat_id):
        return len

    def max_message_length_for_chat(self, chat_id):
        return 4096

    def streaming_overflow_limit(self):
        return None

    def supports_draft_streaming(self, *_a, **_k):
        return False


async def _drive(steps) -> list[str]:
    """Run the consumer through ``steps`` — ("delta", text) or ("break", text|None)."""
    adapter = _EditAdapter()
    consumer = GatewayStreamConsumer(adapter=adapter, chat_id="-100777")
    task = asyncio.create_task(consumer.run())
    await asyncio.sleep(0.05)
    for kind, payload in steps:
        if kind == "delta":
            consumer.on_delta(payload)
        else:
            consumer.on_segment_break(payload)
        await asyncio.sleep(0.04)
    consumer.finish()
    await asyncio.wait_for(task, timeout=5)
    return list(adapter.msgs.values())


@pytest.mark.parametrize("streamed_chars", [4, 23, 34])
@pytest.mark.asyncio
async def test_a_break_while_the_message_is_still_streaming_does_not_split_it(streamed_chars):
    """Whatever fraction had landed, the reader gets one complete sentence."""
    msgs = await _drive([
        ("delta", _SENTENCE[:streamed_chars]),
        ("break", _SENTENCE),                      # already_streamed verdict from a prefix match
        ("delta", _SENTENCE[streamed_chars:]),     # the rest of the SAME message
    ])

    assert len(msgs) == 1, f"sentence split into {len(msgs)}: {msgs}"
    assert msgs[0].strip() == _SENTENCE


@pytest.mark.asyncio
async def test_consecutive_rounds_stay_separate_and_whole():
    """Deferring must not merge rounds: each lands as its own complete message.

    The deferred break fires as soon as its own text has landed, which resets the segment so the
    next round is judged against its own text rather than the previous round's.
    """
    msgs = await _drive([
        ("delta", _ROUND_1[:12]),
        ("break", _ROUND_1),
        ("delta", _ROUND_1[12:]),
        ("delta", "\n\n" + _ROUND_2[:8]),
        ("break", _ROUND_2),
        ("delta", _ROUND_2[8:]),
    ])

    assert len(msgs) == 2, f"expected one message per round, got {msgs}"
    assert any(_ROUND_1 in m for m in msgs)
    assert any(_ROUND_2 in m for m in msgs)


@pytest.mark.asyncio
async def test_a_divergent_break_is_still_honoured():
    """A different message is not an incomplete one — its break must seal as before."""
    msgs = await _drive([
        ("delta", _SENTENCE[:20]),
        ("break", _UNRELATED),
        ("delta", "tail"),
    ])

    assert msgs[0].strip() == _SENTENCE[:20].strip(), "held text must not be rewritten"
    assert len(msgs) == 2, "a divergent break must still start a new message"


@pytest.mark.asyncio
async def test_a_bare_break_keeps_the_previous_behaviour():
    """Callers that pass no text (and every other platform) are unaffected."""
    msgs = await _drive([
        ("delta", _SENTENCE[:15]),
        ("break", None),
        ("delta", _SENTENCE[15:]),
    ])

    assert len(msgs) == 2


@pytest.mark.asyncio
async def test_the_paragraph_break_the_agent_prepends_is_preserved():
    """Whitespace is only collapsed for the comparison, never rewritten in the payload."""
    msgs = await _drive([
        ("delta", "\n\n" + _SENTENCE[:10]),
        ("break", _SENTENCE),
        ("delta", _SENTENCE[10:]),
    ])

    assert len(msgs) == 1
    assert msgs[0].startswith("\n\n")
    assert msgs[0].strip() == _SENTENCE
