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
