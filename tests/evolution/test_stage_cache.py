# -*- coding: utf-8 -*-
"""Unit tests for evolution pipeline node-level caching (issue #2434)."""

import time
from pathlib import Path

import pytest

from evolution.lib.stage_cache import (
    CachePolicy,
    StageCache,
    cache_stage_call,
    compute_stage_cache_key,
)


class TestStageCache:
    """Test suite for StageCache, CachePolicy, and cache_stage_call."""

    def test_compute_stage_cache_key_deterministic(self):
        inputs1 = {"query": "arXiv multi-agent", "limit": 10, "tags": ["agent", "evo"]}
        inputs2 = {"tags": ["agent", "evo"], "limit": 10, "query": "arXiv multi-agent"}

        key1 = compute_stage_cache_key("research", inputs1)
        key2 = compute_stage_cache_key("research", inputs2)
        assert key1 == key2
        assert len(key1) == 64

        # Salt modifies key
        key3 = compute_stage_cache_key("research", inputs1, extra_salt="v2")
        assert key3 != key1

        # Stage modifies key
        key4 = compute_stage_cache_key("analysis", inputs1)
        assert key4 != key1

    def test_in_memory_cache_hit_and_miss(self):
        cache = StageCache()
        inputs = {"date": "2026-08-15", "topic": "causal discovery"}

        # Initial miss
        assert cache.get("research", inputs) is None
        assert cache.stats()["misses"] == 1
        assert cache.stats()["hits"] == 0

        # Set value
        cache.set("research", inputs, {"papers": ["2608.11552"]})
        assert cache.stats()["writes"] == 1

        # Cache hit
        result = cache.get("research", inputs)
        assert result == {"papers": ["2608.11552"]}
        assert cache.stats()["hits"] == 1

    def test_file_backed_persistence(self, tmp_path: Path):
        cache_dir = tmp_path / "evo_cache"
        cache1 = StageCache(cache_dir=cache_dir)
        inputs = {"report_hash": "a1b2c3d4"}

        cache1.set("analysis", inputs, {"verdict": "PROCEED", "score": 0.95})

        # Verify file created
        json_files = list(cache_dir.glob("*.json"))
        assert len(json_files) == 1

        # Second cache instance points to same directory
        cache2 = StageCache(cache_dir=cache_dir)
        hit = cache2.get("analysis", inputs)
        assert hit == {"verdict": "PROCEED", "score": 0.95}
        assert cache2.stats()["hits"] == 1

    def test_ttl_expiration(self):
        cache = StageCache()
        inputs = {"task": "fast_expire"}
        short_policy = CachePolicy(ttl_seconds=0.05)

        cache.set("synthesis", inputs, "output_data", policy=short_policy)
        assert cache.get("synthesis", inputs, policy=short_policy) == "output_data"

        # Wait for expiration
        time.sleep(0.06)
        assert cache.get("synthesis", inputs, policy=short_policy) is None

    def test_stage_cache_invalidation(self, tmp_path: Path):
        cache = StageCache(cache_dir=tmp_path)
        cache.set("research", {"id": 1}, "r1")
        cache.set("research", {"id": 2}, "r2")
        cache.set("analysis", {"id": 1}, "a1")

        assert cache.get("research", {"id": 1}) == "r1"
        assert cache.get("analysis", {"id": 1}) == "a1"

        # Invalidate research stage only
        count = cache.invalidate_stage("research")
        assert count >= 2
        assert cache.get("research", {"id": 1}) is None
        assert cache.get("analysis", {"id": 1}) == "a1"

    def test_cache_stage_call_helper(self):
        cache = StageCache()
        call_count = 0

        def expensive_computation():
            nonlocal call_count
            call_count += 1
            return {"expensive_result": 42}

        # First call: executes function
        res1, was_cached1 = cache_stage_call(
            cache, "research", {"param": 100}, expensive_computation
        )
        assert res1 == {"expensive_result": 42}
        assert was_cached1 is False
        assert call_count == 1

        # Second call: uses cache, does not execute function
        res2, was_cached2 = cache_stage_call(
            cache, "research", {"param": 100}, expensive_computation
        )
        assert res2 == {"expensive_result": 42}
        assert was_cached2 is True
        assert call_count == 1

    def test_cache_policy_for_stage_defaults(self):
        pol_research = CachePolicy.for_stage("research")
        assert pol_research.ttl_seconds == 86400.0

        pol_analysis = CachePolicy.for_stage("analysis")
        assert pol_analysis.ttl_seconds == 7 * 86400.0
