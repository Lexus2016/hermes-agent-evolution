"""Tests for model-identity metadata + model-aware retrieval (issue #2234).

Slice A of contrastive trajectory distillation (#2226). Adds an optional
``model_identity`` field to memory entries and a model-aware retrieval filter
that down-weights entries authored by a different model family.

Backward-compatibility constraints (mirroring #316 provenance):
  * a default add (no model_identity) stays byte-identical to pre-#2234;
  * entries with no model metadata are treated as compatible on retrieval
    (never hidden), so legacy notes keep surfacing;
  * the 3-tuple ``parse_provenance`` contract is preserved.
"""

import sys
import pytest

from tools.memory_tool import (
    MemoryStore,
    DEFAULT_SOURCE_CLASS,
    DEFAULT_TRUST_TIER,
    encode_provenance,
    parse_provenance,
    parse_provenance_full,
)

# Patch get_memory_dir on the SAME module object MemoryStore lives in (see
# test_memory_provenance.py for why a string-path patch is fragile).
_MEM_MODULE = sys.modules[MemoryStore.__module__]


def _use_tmp_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(_MEM_MODULE, "get_memory_dir", lambda: tmp_path)


@pytest.fixture()
def store(tmp_path, monkeypatch):
    _use_tmp_dir(monkeypatch, tmp_path)
    s = MemoryStore(memory_char_limit=2000, user_char_limit=2000)
    s.load_from_disk()
    return s


# ---------------------------------------------------------------------------
# Encoding / parsing round-trip
# ---------------------------------------------------------------------------


class TestModelIdentityEncoding:
    def test_default_add_stays_byte_identical(self):
        # No model_identity + default provenance -> no trailer at all.
        assert encode_provenance("a fact", DEFAULT_SOURCE_CLASS, DEFAULT_TRUST_TIER) == "a fact"

    def test_model_identity_writes_trailer(self):
        stored = encode_provenance("a fact", "agent_authored", "medium", "gpt-4o")
        assert "|model:gpt-4o" in stored
        assert stored.startswith("a fact ")

    def test_model_identity_alone_forces_trailer(self):
        # Even with default provenance, supplying a model must write a trailer.
        stored = encode_provenance("a fact", DEFAULT_SOURCE_CLASS, DEFAULT_TRUST_TIER, "gpt-4o")
        assert "|model:gpt-4o" in stored

    def test_parse_provenance_full_round_trip(self):
        stored = encode_provenance("a fact", "agent_authored", "medium", "gpt-4o")
        text, src, tier, model = parse_provenance_full(stored)
        assert text == "a fact"
        assert src == "agent_authored"
        assert tier == "medium"
        assert model == "gpt-4o"

    def test_parse_provenance_full_no_model(self):
        stored = encode_provenance("a fact", "agent_authored", "medium")
        text, src, tier, model = parse_provenance_full(stored)
        assert model is None
        assert text == "a fact"

    def test_parse_provenance_3tuple_preserved(self):
        stored = encode_provenance("a fact", "agent_authored", "medium", "gpt-4o")
        text, src, tier = parse_provenance(stored)
        assert (text, src, tier) == ("a fact", "agent_authored", "medium")

    def test_legacy_entry_parses_to_none_model(self):
        text, src, tier, model = parse_provenance_full("just a fact")
        assert model is None
        assert (src, tier) == (DEFAULT_SOURCE_CLASS, DEFAULT_TRUST_TIER)


# ---------------------------------------------------------------------------
# Store-level add / search behaviour
# ---------------------------------------------------------------------------


class TestModelAwareRetrieval:
    def test_add_tags_model_identity(self, store):
        store.add("memory", "note from gpt", model_identity="gpt-4o")
        rows = store.search("memory")
        assert rows[0]["model_identity"] == "gpt-4o"
        assert rows[0]["text"] == "note from gpt"

    def test_add_without_model_has_none(self, store):
        store.add("memory", "plain note")
        rows = store.search("memory")
        assert rows[0]["model_identity"] is None

    def test_search_downweights_different_model(self, store):
        store.add("memory", "gpt note", model_identity="gpt-4o")
        store.add("memory", "claude note", model_identity="claude-3")
        store.add("memory", "legacy note")  # no model -> compatible

        rows = store.search("memory", model_identity="gpt-4o")
        texts = [r["text"] for r in rows]
        # gpt note + legacy note are compatible and come first; claude is
        # down-weighted to the end.
        assert texts.index("gpt note") < texts.index("claude note")
        assert texts.index("legacy note") < texts.index("claude note")
        assert set(texts) == {"gpt note", "claude note", "legacy note"}

    def test_search_no_model_returns_all_in_order(self, store):
        store.add("memory", "a", model_identity="gpt-4o")
        store.add("memory", "b", model_identity="claude-3")
        rows = store.search("memory")
        assert [r["text"] for r in rows] == ["a", "b"]

    def test_search_same_model_keeps_order(self, store):
        store.add("memory", "a", model_identity="gpt-4o")
        store.add("memory", "b", model_identity="gpt-4o")
        rows = store.search("memory", model_identity="gpt-4o")
        assert [r["text"] for r in rows] == ["a", "b"]

    def test_replace_retags_model(self, store):
        store.add("memory", "old note", model_identity="gpt-4o")
        store.replace("memory", "old note", "new note", model_identity="claude-3")
        rows = store.search("memory")
        assert rows[0]["text"] == "new note"
        assert rows[0]["model_identity"] == "claude-3"
