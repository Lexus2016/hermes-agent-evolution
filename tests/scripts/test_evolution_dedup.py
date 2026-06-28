"""Tests for scripts/evolution_dedup.py — local idea dedup cache (#91)."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from evolution_dedup import (  # noqa: E402
    idea_key,
    is_seen,
    load_cache,
    normalize_title,
    record,
    save_cache,
)


class TestNormalize:
    def test_strips_tag_and_punctuation_and_case(self):
        # Cosmetic variants map to the same canonical form.
        a = normalize_title("[FIX] Fix the X  problem.")
        b = normalize_title("fix the x problem")
        assert a == b

    def test_different_ideas_differ(self):
        assert normalize_title("[FIX] web scraping 403") != normalize_title("[UX] preflight checks")

    def test_key_stable_and_short(self):
        k = idea_key("[IMPROVEMENT] Per-cycle funnel metrics")
        assert k == idea_key("per-cycle funnel metrics")
        assert len(k) == 16


class TestCacheRoundTrip:
    def test_record_then_seen(self, tmp_path):
        cache = {}
        record(cache, "[FIX] Something broke", "filed", issue=42, date="2026-06-13")
        assert is_seen(cache, "something broke")  # normalized match
        assert not is_seen(cache, "a totally different idea")

    def test_save_load(self, tmp_path):
        p = tmp_path / "dedup-cache.json"
        cache = {}
        record(cache, "Idea one", "filed", issue=1, date="2026-06-12")
        save_cache(p, cache)
        loaded = load_cache(p)
        assert is_seen(loaded, "idea one")
        assert loaded[idea_key("Idea one")]["issue"] == 1

    def test_missing_file_is_empty(self, tmp_path):
        assert load_cache(tmp_path / "nope.json") == {}

    def test_malformed_file_is_empty(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("{ not json", encoding="utf-8")
        assert load_cache(p) == {}

    def test_cap_keeps_newest(self, tmp_path):
        from evolution_dedup import _MAX_ENTRIES

        p = tmp_path / "c.json"
        cache = {}
        for i in range(_MAX_ENTRIES + 50):
            # date ordering: later i -> later date so newest survive
            record(cache, f"idea number {i}", "considered", date=f"2026-06-{(i % 28) + 1:02d}")
        # force distinct, monotonic dates so the cap is deterministic
        for i, k in enumerate(list(cache)):
            cache[k]["date"] = f"{2000 + i:04d}-01-01"
        save_cache(p, cache)
        loaded = load_cache(p)
        assert len(loaded) == _MAX_ENTRIES
