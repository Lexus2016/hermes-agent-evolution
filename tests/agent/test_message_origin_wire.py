# -*- coding: utf-8 -*-
"""``origin`` is persistence-only and must never reach a provider.

Message provenance rides resumed history exactly the way ``display_kind`` and
``api_content`` do. A strict OpenAI-compatible backend rejects an unknown
message key outright — the same class of failure as the ``timestamp`` leak
(#47868), which is why the transport already carries an explicit list of
persistence-only fields to strip.

These tests exist because the leak is invisible in normal use: it only shows up
against a strict provider, after a resume, on a row that happens to carry
provenance.
"""

from __future__ import annotations

import pytest

from agent.transports.chat_completions import ChatCompletionsTransport


def _convert(messages):
    transport = ChatCompletionsTransport.__new__(ChatCompletionsTransport)
    return transport.convert_messages(messages)


class TestOriginNeverReachesTheWire:
    def test_origin_alone_is_stripped(self):
        out = _convert([{"role": "user", "content": "x", "origin": "human"}])
        assert out == [{"role": "user", "content": "x"}]

    @pytest.mark.parametrize("value", ["human", "runtime", "api", "bogus"])
    def test_every_value_is_stripped(self, value):
        out = _convert([{"role": "user", "content": "x", "origin": value}])
        assert "origin" not in out[0]

    def test_stripped_alongside_the_other_persistence_fields(self):
        """`display_kind` is deliberately not asserted here: this transport
        strips a specific list and that field is removed further up, in the
        conversation loop's provider clone."""
        out = _convert(
            [
                {
                    "role": "user",
                    "content": "x",
                    "origin": "human",
                    "api_content": "y",
                    "timestamp": 1.0,
                    "_row_id": 7,
                }
            ]
        )
        assert out == [{"role": "user", "content": "x"}]

    def test_a_clean_message_is_untouched(self):
        msg = {"role": "assistant", "content": "hello"}
        assert _convert([msg]) == [msg]

    def test_the_input_list_is_not_mutated(self):
        """Stripping must happen on a copy: the live transcript keeps its
        provenance, or the next persistence write would lose it."""
        original = {"role": "user", "content": "x", "origin": "human"}
        _convert([original])
        assert original["origin"] == "human"


class TestApiCloneStripsOrigin:
    """The conversation loop builds its own provider-bound clone."""

    def test_loop_clone_pops_origin(self):
        import inspect

        from agent import conversation_loop

        source = inspect.getsource(conversation_loop)
        assert 'api_msg.pop("origin", None)' in source, (
            "the loop's provider clone must strip provenance the way it strips "
            "display_kind and api_content"
        )

    def test_context_compressor_carrier_projection_pops_origin(self):
        """The compressor's model-facing carrier projection is a provider
        boundary; the display projection in compaction_display is not, and
        returns non-summary messages untouched by design."""
        import inspect

        from agent import context_compressor

        source = inspect.getsource(context_compressor)
        assert 'candidate.pop("origin", None)' in source


class TestSummaryPathStripsOrigin:
    """The iteration-limit summary hand-builds its request and calls
    chat.completions.create() directly, bypassing the transport — so it has to
    mirror the same sanitization or a resumed provenance-tagged row reaches a
    strict provider as an unknown key.
    """

    def test_origin_is_in_the_schema_foreign_list(self):
        import inspect

        from agent import chat_completion_helpers

        source = inspect.getsource(chat_completion_helpers)
        marker = source.split("for schema_foreign in (")[1].split("):")[0]
        assert '"origin"' in marker, (
            "the summary path must strip origin alongside timestamp and "
            "platform_message_id"
        )
