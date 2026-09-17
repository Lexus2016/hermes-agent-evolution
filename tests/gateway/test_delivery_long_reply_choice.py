"""The reader chooses how an over-cap reply arrives — split messages or one file.

The gateway must not decide this for a human. So the first time a chat receives a reply that does
not fit one message, it is asked once; the answer is remembered and nothing is asked again. Until it
answers, delivery behaves exactly as it did before, so the prompt cannot change what an unanswered
chat receives.
"""

from types import SimpleNamespace

import pytest

from gateway import long_reply_pref
from gateway.config import GatewayConfig, Platform
from gateway.delivery import DeliveryRouter, DeliveryTarget

_LONG = "x" * 9000       # over MAX_PLATFORM_OUTPUT
_SHORT = "a short reply"


class _Adapter:
    """Capability-complete fake: can split, attach a document and ask a question."""

    splits_long_messages = True

    def __init__(self, document_succeeds: bool = True) -> None:
        self.sends: list = []
        self.documents: list = []
        self.pickers: list = []
        self._document_succeeds = document_succeeds

    async def send(self, chat_id, content, metadata=None):
        self.sends.append(content)
        return {"success": True}

    async def send_document(self, chat_id=None, file_path=None, caption=None, metadata=None):
        body = open(file_path, encoding="utf-8").read()
        self.documents.append({"caption": caption, "body": body, "name": file_path})
        return SimpleNamespace(success=self._document_succeeds, message_id="doc-1", error="nope")

    async def send_choice_picker(self, chat_id, title, choices, session_key, on_choice, metadata=None):
        self.pickers.append({"title": title, "choices": choices, "on_choice": on_choice})
        return SimpleNamespace(success=True, message_id="pick-1")


class _NoDocumentAdapter(_Adapter):
    """An adapter that cannot attach files must never be asked the question."""
    send_document = None


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(long_reply_pref, "_store_path", lambda: str(tmp_path / "prefs.json"))
    monkeypatch.setattr("gateway.delivery.get_hermes_home", lambda: tmp_path)


def _router(adapter):
    return DeliveryRouter(GatewayConfig(), adapters={Platform.DISCORD: adapter})


@pytest.mark.asyncio
async def test_first_long_reply_is_delivered_normally_then_asks_once():
    """The report goes out unchanged; the question comes after it, never instead of it."""
    adapter = _Adapter()
    router = _router(adapter)
    target = DeliveryTarget.parse("discord:12345")

    await router._deliver_to_platform(target, _LONG, metadata=None)

    assert adapter.sends == [_LONG], "delivery must be untouched by an unanswered prompt"
    assert adapter.documents == []
    assert len(adapter.pickers) == 1
    assert {c["value"] for c in adapter.pickers[0]["choices"]} == {"split", "document"}
    # "Split" is marked as the status quo so the reader sees what they have now.
    assert next(c for c in adapter.pickers[0]["choices"] if c["value"] == "split")["is_current"]


@pytest.mark.asyncio
async def test_the_question_is_asked_only_once():
    adapter = _Adapter()
    router = _router(adapter)
    target = DeliveryTarget.parse("discord:12345")

    await router._deliver_to_platform(target, _LONG, metadata=None)
    await router._deliver_to_platform(target, _LONG, metadata=None)

    assert len(adapter.pickers) == 1, "a one-time courtesy must not become a recurring prompt"
    assert len(adapter.sends) == 2


@pytest.mark.asyncio
async def test_choosing_a_file_changes_later_deliveries():
    adapter = _Adapter()
    router = _router(adapter)
    target = DeliveryTarget.parse("discord:12345")

    await router._deliver_to_platform(target, _LONG, metadata=None)
    confirmation = await adapter.pickers[0]["on_choice"]("12345", "document")

    assert "file" in confirmation.lower()
    assert long_reply_pref.get_preference("discord", "12345") == "document"

    await router._deliver_to_platform(target, _LONG, metadata=None)

    assert len(adapter.documents) == 1
    assert adapter.documents[0]["body"] == _LONG, "the file must hold the whole reply"
    assert adapter.documents[0]["name"].endswith(".md")
    assert len(adapter.sends) == 1, "the chosen file replaces the split messages, not joins them"


@pytest.mark.asyncio
async def test_short_replies_are_never_touched():
    adapter = _Adapter()
    router = _router(adapter)
    target = DeliveryTarget.parse("discord:12345")

    await router._deliver_to_platform(target, _SHORT, metadata=None)

    assert adapter.sends == [_SHORT]
    assert adapter.pickers == []
    assert not long_reply_pref.was_asked("discord", "12345")


@pytest.mark.asyncio
async def test_a_failed_document_send_falls_back_to_messages():
    """Never lose a report because the reader's preferred shape failed."""
    adapter = _Adapter(document_succeeds=False)
    router = _router(adapter)
    target = DeliveryTarget.parse("discord:12345")
    long_reply_pref.set_preference("discord", "12345", "document")

    await router._deliver_to_platform(target, _LONG, metadata=None)

    assert adapter.sends == [_LONG], "the report must still arrive"


@pytest.mark.asyncio
async def test_adapter_without_attachments_is_never_asked():
    adapter = _NoDocumentAdapter()
    router = _router(adapter)
    target = DeliveryTarget.parse("discord:12345")

    await router._deliver_to_platform(target, _LONG, metadata=None)

    assert adapter.pickers == []
    assert adapter.sends == [_LONG]


def test_unknown_preference_is_refused_and_silence_is_not_consent():
    assert long_reply_pref.set_preference("discord", "1", "carrier-pigeon") is False
    assert long_reply_pref.get_preference("discord", "1") is None
    # Asked but unanswered: still no preference, so delivery keeps the old behaviour.
    long_reply_pref.mark_asked("discord", "1")
    assert long_reply_pref.was_asked("discord", "1") is True
    assert long_reply_pref.get_preference("discord", "1") is None
