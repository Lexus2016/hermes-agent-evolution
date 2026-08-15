"""Tests for tools.memory_governance — issue #2437.

Covers: trailer codec round-trips, backward compatibility (legacy entries
parse active), malformed-trailer safety, idempotent supersession, ambiguity
contract, and governed_search (supersession-aware + provenance-gated recall
over a stub store).
"""

import sys

import pytest

from tools.memory_governance import (
    SUP_STATUS,
    encode_supersession,
    governed_search,
    is_superseded,
    parse_supersession,
    supersede_entry,
    utc_now_iso,
)

TS = "2026-08-15T00:00:00Z"


# ── Codec round-trips ───────────────────────────────────────────────────


class TestCodec:
    def test_round_trip(self):
        stored = encode_supersession("stage=research done", TS)
        display, ts = parse_supersession(stored)
        assert display == "stage=research done"
        assert ts == TS

    def test_legacy_entry_is_active(self):
        display, ts = parse_supersession("plain old entry")
        assert display == "plain old entry"
        assert ts is None
        assert not is_superseded("plain old entry")

    def test_default_add_is_active(self):
        assert not is_superseded("")

    def test_malformed_trailer_is_content(self):
        bad = "entry ⟦sup:not-a-real-ts|status:bogus⟧"
        display, ts = parse_supersession(bad)
        assert ts is None
        assert "⟦sup:" in display  # stays part of the text

    def test_missing_status_segment_is_content(self):
        bad = "entry ⟦sup:2026-08-15T00:00:00Z⟧"
        _, ts = parse_supersession(bad)
        assert ts is None

    def test_encode_is_idempotent_first_ts_wins(self):
        once = encode_supersession("e", TS)
        twice = encode_supersession(once, "2030-01-01T00:00:00Z")
        assert once == twice
        assert parse_supersession(once)[1] == TS

    def test_trailer_stacks_after_provenance(self):
        combined = "fact ⟦src:agent_authored|trust:low⟧"
        stored = encode_supersession(combined, TS)
        # Outermost trailer is supersession; provenance survives underneath.
        display, ts = parse_supersession(stored)
        assert ts == TS
        assert display == "fact ⟦src:agent_authored|trust:low⟧"

    def test_utc_now_iso_shape(self):
        s = utc_now_iso()
        assert s.endswith("Z") and "T" in s and len(s) == 20


# ── supersede_entry ─────────────────────────────────────────────────────


class TestSupersedeEntry:
    def test_supersede_first_match(self):
        entries = ["alpha stage=pending", "beta stage=done"]
        res = supersede_entry(entries, "alpha", TS)
        assert res["success"] and res["superseded_at"] == TS
        assert is_superseded(entries[0])
        assert not is_superseded(entries[1])

    def test_already_superseded_is_noop(self):
        entries = [encode_supersession("alpha", TS)]
        res = supersede_entry(entries, "alpha", "2030-01-01T00:00:00Z")
        assert res["success"] and res.get("already_superseded")
        assert parse_supersession(entries[0])[1] == TS

    def test_no_match(self):
        res = supersede_entry(["alpha"], "zzz")
        assert not res["success"]
        assert "No entry matched" in res["error"]

    def test_ambiguous_match_rejected(self):
        entries = ["alpha one", "alpha two"]
        res = supersede_entry(entries, "alpha")
        assert not res["success"]
        assert "Multiple distinct entries" in res["error"]

    def test_empty_match_rejected(self):
        assert not supersede_entry(["x"], "")["success"]


# ── governed_search ─────────────────────────────────────────────────────


class _StubStore:
    """MemoryStore-like stub: search() mimics the #316 retrieval path
    (source_filter + min_trust enforcement, provenance trailer stripped
    from display text). A sup trailer therefore rides INSIDE the row text
    for combined entries — the pre-integration reality governed_search
    defends against."""

    def __init__(self, entries):
        self._entries = list(entries)

    def search(self, target, source_filter=None, min_trust=None):
        from tools.memory_tool import _trust_rank, parse_provenance

        if isinstance(source_filter, str):
            allowed = {source_filter}
        elif source_filter is None:
            allowed = None
        else:
            allowed = set(source_filter)
        min_rank = _trust_rank(min_trust) if min_trust is not None else None
        rows = []
        for e in self._entries:
            text, src, tier = parse_provenance(e)
            if allowed is not None and src not in allowed:
                continue
            if min_rank is not None and _trust_rank(tier) < min_rank:
                continue
            rows.append({"text": text, "source_class": src, "trust_tier": tier})
        return rows


class TestGovernedSearch:
    def test_superseded_hidden_by_default(self):
        store = _StubStore(
            [
                encode_supersession("stage=analysis status=done", TS),
                "stage=implementation status=pending",
            ]
        )
        rows = governed_search(store, "memory")
        assert [r["text"] for r in rows] == ["stage=implementation status=pending"]

    def test_include_superseded_shows_ts(self):
        store = _StubStore([encode_supersession("old fact", TS)])
        rows = governed_search(store, "memory", include_superseded=True)
        assert len(rows) == 1
        assert rows[0]["superseded_at"] == TS
        assert rows[0]["text"] == "old fact"

    def test_provenance_filter_passthrough(self):
        store = _StubStore(
            [
                "trusted fact ⟦src:user_input|trust:trusted⟧",
                "guess ⟦src:agent_authored|trust:low⟧",
            ]
        )
        rows = governed_search(store, "memory", min_trust="trusted")
        assert [r["text"] for r in rows] == ["trusted fact"]
        rows_all = governed_search(store, "memory", min_trust="low")
        assert len(rows_all) == 2

    def test_source_filter_passthrough(self):
        store = _StubStore(
            [
                "user said so ⟦src:user_input|trust:trusted⟧",
                "tool said ⟦src:external_tool|trust:medium⟧",
            ]
        )
        rows = governed_search(store, "memory", source_filter={"user_input"})
        assert [r["text"] for r in rows] == ["user said so"]

    def test_combined_provenance_and_supersession(self):
        # Combined entry: after governance stripping the #316 parse of the
        # stored form degrades to source/trust "unknown" (documented safe
        # direction) — so this case filters on supersession only.
        stale = encode_supersession("gate=open ⟦src:agent_authored|trust:low⟧", TS)
        fresh = "gate=closed ⟦src:user_input|trust:trusted⟧"
        store = _StubStore([stale, fresh])
        rows = governed_search(store, "memory")
        # stale is superseded → hidden from default recall; caller must
        # explicitly opt in to see retired rows.
        assert [r["text"] for r in rows] == ["gate=closed"]
        all_rows = governed_search(store, "memory", include_superseded=True)
        assert len(all_rows) == 2
        sup_rows = [r for r in all_rows if r.get("superseded_at")]
        assert len(sup_rows) == 1 and sup_rows[0]["text"] == "gate=open ⟦src:agent_authored|trust:low⟧"

    def test_empty_store(self):
        assert governed_search(_StubStore([]), "memory") == []


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
