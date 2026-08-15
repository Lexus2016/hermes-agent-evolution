"""Tests for the memory-governance codec (#2437).

Covers the standalone ``tools/memory_governance`` module: provenance-gated
recall via temporal supersession markers, plus the outermost-trailer parse
ordering that keeps it safe against malformed input.
"""

from __future__ import annotations

from tools.memory_governance import (
    encode_supersession,
    parse_supersession,
    supersede_entry,
)


class TestSupersessionCodec:
    def test_roundtrip_marker(self):
        text = "old fact"
        enc = encode_supersession(text, timestamp="2026-08-15T11:23:00Z")
        assert enc.endswith("⟧")
        display, ts = parse_supersession(enc)
        assert display == "old fact"
        assert ts == "2026-08-15T11:23:00Z"

    def test_marker_is_outermost_before_provenance(self):
        # Provenance trailer (⟦src:…|trust:…⟧) sits INSIDE the sup marker.
        provenanced = "old fact ⟦src:external_tool|trust:medium⟧"
        enc = encode_supersession(provenanced, timestamp="2026-08-15T11:23:00Z")
        display, ts = parse_supersession(enc)
        # parse_supersession strips ONLY the outermost sup marker; the
        # provenance trailer must still be present for memory_tool to parse.
        assert ts == "2026-08-15T11:23:00Z"
        assert "⟦src:external_tool|trust:medium⟧" in display

    def test_no_marker_returns_none(self):
        assert parse_supersession("plain entry") == ("plain entry", None)

    def test_malformed_marker_degrades_to_not_superseded(self):
        bad = "entry ⟦sup:not-a-date⟧"
        display, ts = parse_supersession(bad)
        assert ts is None
        # malformed trailer is left as ordinary content, never guessed
        assert display == bad or display == "entry ⟦sup:not-a-date⟧"

    def test_first_supersession_wins(self):
        entries = ["stale fact about X"]
        r = supersede_entry(entries, "fact about X", timestamp="2026-08-15T01:00:00Z")
        assert r["success"] is True
        assert r["already_superseded"] is False

        # second supersession of the same entry keeps the original timestamp
        r2 = supersede_entry(entries, "fact about X", timestamp="2026-08-15T02:00:00Z")
        assert r2["success"] is True
        assert r2["already_superseded"] is True
        assert r2["superseded_at"] == "2026-08-15T01:00:00Z"

    def test_supersede_mutates_in_place_and_keeps_others(self):
        entries = ["keep me", "stale other"]
        r = supersede_entry(entries, "stale other", timestamp="2026-08-15T03:00:00Z")
        assert r["success"] is True
        assert len(entries) == 2
        assert entries[0] == "keep me"
        _, ts = parse_supersession(entries[1])
        assert ts == "2026-08-15T03:00:00Z"

    def test_supersede_missing_entry(self):
        entries = ["one", "two"]
        r = supersede_entry(entries, "nope")
        assert r["success"] is False

    def test_supersede_empty_old_text(self):
        entries = ["one"]
        r = supersede_entry(entries, "")
        assert r["success"] is False


class TestGovernedSearch:
    def test_can_import_governed_search(self):
        from tools.memory_governance import governed_search

        assert callable(governed_search)

    def test_search_hides_superseded_by_default(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        from tools.memory_tool import MemoryStore

        store = MemoryStore()
        store.add("memory", "active note about python")
        store.add("memory", "stale note about ruby")

        # Supersede ruby note
        res = store.supersede("memory", "stale note about ruby")
        assert res["success"] is True

        # Default search hides superseded entry
        results = store.search("memory")
        texts = [r["text"] for r in results]
        assert "active note about python" in texts
        assert not any("ruby" in t for t in texts)

        # include_superseded=True returns it with timestamp
        all_results = store.search("memory", include_superseded=True)
        assert len(all_results) == 2
        ruby_row = next(r for r in all_results if "ruby" in r["text"])
        assert ruby_row["superseded_at"] is not None
        python_row = next(r for r in all_results if "python" in r["text"])
        assert python_row.get("superseded_at") is None

    def test_memory_tool_supersede_action_e2e(self, tmp_path, monkeypatch):
        import json

        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        from tools.memory_tool import MemoryStore, memory_tool

        store = MemoryStore()

        # Add an entry
        r1 = json.loads(
            memory_tool(
                action="add",
                target="memory",
                content="fact A to supersede",
                store=store,
            )
        )
        assert r1["success"] is True

        # Supersede it
        r2 = json.loads(
            memory_tool(
                action="supersede", target="memory", old_text="fact A", store=store
            )
        )
        assert r2["success"] is True
        assert "superseded" in r2["message"].lower()

        # Supersede again (first-supersession-wins)
        r3 = json.loads(
            memory_tool(
                action="supersede", target="memory", old_text="fact A", store=store
            )
        )
        assert r3["success"] is True
        assert "already superseded" in r3["message"].lower()

        # Search via tool with include_superseded
        r4 = json.loads(
            memory_tool(
                action="search", target="memory", include_superseded=True, store=store
            )
        )
        assert r4["success"] is True
        assert len(r4["results"]) == 1
        assert r4["results"][0].get("superseded_at") is not None
