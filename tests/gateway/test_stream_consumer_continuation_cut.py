"""A fallback continuation must not begin mid-word.

When edits stop working (Telegram flood control), the consumer sends only the part the reader has
not seen, cut at whatever the last successful EDIT put on screen. Edits fire on a throttle tick, not
at a word boundary, so that cut lands inside a word and one word is published split across two
messages — observed in production as 'рядкам' + 'и (мій попередній…'.
"""

import asyncio
from types import SimpleNamespace

import pytest

from gateway.stream_consumer import GatewayStreamConsumer

_TEXT = "Правлю точними живими рядками (мій попередній EN-паттерн не збігався):"


class _FloodingAdapter:
    """Lets the first edit land, then refuses every edit the way flood control does."""

    MAX_MESSAGE_LENGTH = 4096
    splits_long_messages = True
    FALLBACK_ON_FINAL_EDIT_FLOOD = True

    def __init__(self, edits_before_flood: int = 1) -> None:
        self.msgs: dict[str, str] = {}
        self._n = 0
        self.edits = 0
        self._allow = edits_before_flood

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self._n += 1
        mid = f"m{self._n}"
        self.msgs[mid] = content
        return SimpleNamespace(success=True, message_id=mid)

    async def edit_message(self, chat_id, message_id, content, *, finalize=False, metadata=None):
        self.edits += 1
        if self.edits > self._allow:
            return SimpleNamespace(success=False, error="flood_control:34.0",
                                   retry_after=34.0, message_id=message_id)
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


@pytest.mark.asyncio
async def test_a_flood_driven_continuation_does_not_start_mid_word():
    adapter = _FloodingAdapter()
    consumer = GatewayStreamConsumer(adapter=adapter, chat_id="-100777")
    task = asyncio.create_task(consumer.run())
    await asyncio.sleep(0.05)
    for i in range(0, len(_TEXT), 4):
        consumer.on_delta(_TEXT[i:i + 4])
        await asyncio.sleep(0.03)
    consumer.finish()
    await asyncio.wait_for(task, timeout=10)

    msgs = list(adapter.msgs.values())
    assert len(msgs) == 2, f"expected a preview plus a continuation, got {msgs}"
    continuation = msgs[1].lstrip()
    assert continuation, "the remainder must still be delivered"
    # The continuation opens on a word of its own, not on the tail of a severed one.
    first_word = continuation.split()[0]
    assert _TEXT.split().count(first_word) or first_word in _TEXT.split(), (
        f"continuation starts mid-word: {continuation[:30]!r}")
    assert " ".join(_TEXT.split()).endswith(" ".join(continuation.split())), (
        "the continuation must be a suffix of the message, whole words included")


@pytest.mark.parametrize(
    "text, cut, expected_start",
    [
        ("один два три", 4, 4),        # the cut IS the space — already a boundary, untouched
        ("один два три", 6, 5),        # inside "два" — back up to its start
        ("один два три", 9, 9),        # start of "три" is itself a boundary
        ("рядками (мій", 4, 4),        # inside the FIRST word: no boundary behind it, keep the cut
        ("непроривнедовгеслово", 5, 5),  # one long token — keep the cut rather than resend it all
    ],
)
def test_word_safe_cut(text, cut, expected_start):
    assert GatewayStreamConsumer._word_safe_cut(text, cut) == expected_start
