"""Tests for source-provenance tagging on memory entries (issue #316).

Backward-compatibility is the hard constraint here. The §-delimited
MEMORY.md / USER.md files predate provenance, so:

  * old entries (no provenance trailer) MUST load unchanged and default to
    safe provenance (source_class="unknown", trust_tier="unknown");
  * a *default* add (no source_class given) must stay byte-identical to the
    pre-#316 behaviour — no trailer written, no-filter retrieval returns the
    exact same strings as before;
  * explicit source_class / trust_tier must round-trip through disk;
  * the new retrieval filter (source_filter / min_trust) selects a subset,
    and the no-filter call returns everything.

This slice is *tagging only* — the block / warn / strip guard that consumes
these tags is issue #315.
"""

import sys
import pytest

from tools.memory_tool import (
    MemoryStore,
    SOURCE_CLASSES,
    TRUST_TIERS,
    DEFAULT_SOURCE_CLASS,
    DEFAULT_TRUST_TIER,
    parse_provenance,
    ENTRY_DELIMITER,
)

# Patch get_memory_dir on the SAME module object MemoryStore lives in, not on
# the "tools.memory_tool" string. test_memory_tool_import_fallback.py reimports
# the module and swaps sys.modules["tools.memory_tool"]; a string-path patch
# could then land on the wrong module object (pre-existing repo fragility).
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
# Constants / vocabulary
# ---------------------------------------------------------------------------

class TestProvenanceVocabulary:
    def test_source_classes_match_issue(self):
        assert set(SOURCE_CLASSES) == {
            "user_input",
            "external_tool",
            "agent_authored",
            "system",
            "unknown",
        }

    def test_trust_tiers_ordered(self):
        # tiers must be ordered low->high so min_trust comparisons work
        assert TRUST_TIERS.index("untrusted") < TRUST_TIERS.index("trusted")

    def test_safe_defaults(self):
        # Safe defaults: unknown source, lowest meaningful trust.
        assert DEFAULT_SOURCE_CLASS == "unknown"
        assert DEFAULT_TRUST_TIER == "unknown"


# ---------------------------------------------------------------------------
# Default add — must NOT change on-disk bytes vs pre-#316
# ---------------------------------------------------------------------------

class TestDefaultAddByteCompatible:
    def test_default_add_writes_plain_string(self, store, tmp_path):
        """A default add (no provenance) writes the entry verbatim, no trailer."""
        store.add("memory", "Project uses Python 3.12")
        raw = (tmp_path / "MEMORY.md").read_text(encoding="utf-8")
        # Byte-identical to the pre-#316 serialization: just the content.
        assert raw == "Project uses Python 3.12"
        assert "⟦" not in raw  # no provenance sentinel

    def test_default_add_has_safe_default_provenance(self, store):
        store.add("memory", "fact A")
        rows = store.search("memory")
        assert len(rows) == 1
        assert rows[0]["text"] == "fact A"
        assert rows[0]["source_class"] == DEFAULT_SOURCE_CLASS
        assert rows[0]["trust_tier"] == DEFAULT_TRUST_TIER

    def test_default_add_entries_list_unchanged(self, store):
        """The legacy ``entries`` field in the response stays plain strings."""
        result = store.add("memory", "legacy shape")
        assert result["entries"] == ["legacy shape"]


# ---------------------------------------------------------------------------
# Explicit provenance round-trips through disk
# ---------------------------------------------------------------------------

class TestExplicitProvenanceRoundTrip:
    def test_add_with_source_class_round_trips(self, store, tmp_path, monkeypatch):
        store.add(
            "memory",
            "User said deploy on Fridays",
            source_class="user_input",
            trust_tier="trusted",
        )
        # New store re-reads from disk -> provenance survives.
        _use_tmp_dir(monkeypatch, tmp_path)
        s2 = MemoryStore(memory_char_limit=2000, user_char_limit=2000)
        s2.load_from_disk()
        rows = s2.search("memory")
        assert len(rows) == 1
        assert rows[0]["text"] == "User said deploy on Fridays"
        assert rows[0]["source_class"] == "user_input"
        assert rows[0]["trust_tier"] == "trusted"

    def test_display_text_strips_trailer(self, store):
        """The user-visible text never leaks the provenance sentinel."""
        store.add(
            "memory",
            "agent guess about the API",
            source_class="agent_authored",
            trust_tier="untrusted",
        )
        rows = store.search("memory")
        assert rows[0]["text"] == "agent guess about the API"
        assert "⟦" not in rows[0]["text"]
        assert "src:" not in rows[0]["text"]

    def test_replace_records_provenance(self, store):
        store.add("memory", "old draft", source_class="agent_authored")
        store.replace(
            "memory",
            "old draft",
            "verified fact",
            source_class="external_tool",
            trust_tier="trusted",
        )
        rows = store.search("memory")
        assert len(rows) == 1
        assert rows[0]["text"] == "verified fact"
        assert rows[0]["source_class"] == "external_tool"
        assert rows[0]["trust_tier"] == "trusted"

    def test_invalid_source_class_rejected(self, store):
        result = store.add("memory", "x", source_class="not_a_class")
        assert result["success"] is False
        assert "source_class" in result["error"]

    def test_invalid_trust_tier_rejected(self, store):
        result = store.add("memory", "x", trust_tier="super_duper")
        assert result["success"] is False
        assert "trust_tier" in result["error"]


# ---------------------------------------------------------------------------
# Old entries without fields still load
# ---------------------------------------------------------------------------

class TestOldEntriesLoad:
    def test_old_file_loads_unchanged(self, tmp_path, monkeypatch):
        _use_tmp_dir(monkeypatch, tmp_path)
        # A pre-#316 file: plain strings joined by the delimiter, no trailers.
        legacy = "Entry one\n§\nEntry two\n§\nEntry three"
        (tmp_path / "MEMORY.md").write_text(legacy, encoding="utf-8")

        s = MemoryStore(memory_char_limit=2000, user_char_limit=2000)
        s.load_from_disk()

        # Live state preserves the exact legacy strings.
        assert s.memory_entries == ["Entry one", "Entry two", "Entry three"]
        # And they get safe-default provenance at retrieval.
        rows = s.search("memory")
        assert [r["text"] for r in rows] == ["Entry one", "Entry two", "Entry three"]
        for r in rows:
            assert r["source_class"] == DEFAULT_SOURCE_CLASS
            assert r["trust_tier"] == DEFAULT_TRUST_TIER

    def test_old_file_bytes_preserved_through_noop_reload(self, tmp_path, monkeypatch):
        """Loading a legacy file does not rewrite it (no silent migration)."""
        _use_tmp_dir(monkeypatch, tmp_path)
        legacy = "Legacy A\n§\nLegacy B"
        path = tmp_path / "MEMORY.md"
        path.write_text(legacy, encoding="utf-8")
        before = path.read_text(encoding="utf-8")

        s = MemoryStore(memory_char_limit=2000, user_char_limit=2000)
        s.load_from_disk()

        assert path.read_text(encoding="utf-8") == before

    def test_parse_provenance_on_bare_string(self):
        text, sc, tt = parse_provenance("just a fact")
        assert text == "just a fact"
        assert sc == DEFAULT_SOURCE_CLASS
        assert tt == DEFAULT_TRUST_TIER


# ---------------------------------------------------------------------------
# Retrieval filtering by source / trust
# ---------------------------------------------------------------------------

class TestRetrievalFilter:
    def _seed(self, store):
        store.add("memory", "from the user", source_class="user_input", trust_tier="trusted")
        store.add("memory", "from a tool", source_class="external_tool", trust_tier="trusted")
        store.add("memory", "agent hunch", source_class="agent_authored", trust_tier="untrusted")
        store.add("memory", "legacy note")  # defaults: unknown / unknown

    def test_no_filter_returns_all(self, store):
        self._seed(store)
        rows = store.search("memory")
        texts = {r["text"] for r in rows}
        assert texts == {"from the user", "from a tool", "agent hunch", "legacy note"}

    def test_source_filter_single(self, store):
        self._seed(store)
        rows = store.search("memory", source_filter="agent_authored")
        assert [r["text"] for r in rows] == ["agent hunch"]

    def test_source_filter_list(self, store):
        self._seed(store)
        rows = store.search("memory", source_filter=["user_input", "external_tool"])
        assert {r["text"] for r in rows} == {"from the user", "from a tool"}

    def test_min_trust_excludes_lower(self, store):
        self._seed(store)
        rows = store.search("memory", min_trust="trusted")
        # Only the two "trusted" entries clear the bar; untrusted + unknown drop.
        assert {r["text"] for r in rows} == {"from the user", "from a tool"}

    def test_min_trust_and_source_filter_combine(self, store):
        self._seed(store)
        rows = store.search(
            "memory", source_filter=["user_input", "agent_authored"], min_trust="trusted"
        )
        assert [r["text"] for r in rows] == ["from the user"]

    def test_tool_search_action_returns_filtered(self, store):
        import json
        from tools.memory_tool import memory_tool

        self._seed(store)
        out = json.loads(
            memory_tool(action="search", target="memory", source_filter="user_input", store=store)
        )
        assert out["success"] is True
        assert [r["text"] for r in out["results"]] == ["from the user"]
        # Provenance surfaced for the consumer (the #315 guard).
        assert out["results"][0]["source_class"] == "user_input"


# ---------------------------------------------------------------------------
# No-filter byte-compat: a default-only store round-trips identically
# ---------------------------------------------------------------------------

class TestNoFilterByteCompatible:
    def test_default_only_store_disk_is_plain(self, store, tmp_path):
        store.add("memory", "alpha")
        store.add("memory", "beta")
        raw = (tmp_path / "MEMORY.md").read_text(encoding="utf-8")
        # Exactly what a pre-#316 store would have written.
        assert raw == "alpha" + ENTRY_DELIMITER + "beta"
