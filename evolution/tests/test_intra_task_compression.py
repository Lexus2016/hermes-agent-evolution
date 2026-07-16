# -*- coding: utf-8 -*-
"""Tests for intra_task_compression module (Issue #1033)."""

import pytest
from lib.intra_task_compression import (
    CompressionTrigger,
    CompressionConfig,
    TokenEstimator,
    CompressionTriggerEvaluator,
    CompressionResult,
    KnowledgeBlockExtractor,
    IntraTaskCompressor,
)


# ===========================================================================
# CompressionTrigger enum
# ===========================================================================

class TestCompressionTrigger:
    def test_values(self):
        assert CompressionTrigger.TOKEN_THRESHOLD.value == "token_threshold"
        assert CompressionTrigger.MANUAL.value == "manual"
        assert CompressionTrigger.CONTEXT_PRESSURE.value == "context_pressure"
        assert CompressionTrigger.NONE.value == "none"

    def test_distinct_values(self):
        vals = [t.value for t in CompressionTrigger]
        assert len(vals) == len(set(vals))

    def test_from_value(self):
        assert CompressionTrigger("token_threshold") == CompressionTrigger.TOKEN_THRESHOLD


# ===========================================================================
# CompressionConfig
# ===========================================================================

class TestCompressionConfig:
    def test_defaults(self):
        c = CompressionConfig()
        assert c.enabled is True
        assert c.token_threshold == 8000
        assert c.compression_ratio == 0.3
        assert c.preserve_recent_count == 5
        assert c.preserve_system_messages is True
        assert c.max_compressions_per_task == 3

    def test_custom_values(self):
        c = CompressionConfig(enabled=False, token_threshold=4000, compression_ratio=0.5)
        assert c.enabled is False
        assert c.token_threshold == 4000
        assert c.compression_ratio == 0.5

    def test_to_dict(self):
        c = CompressionConfig(token_threshold=10000)
        d = c.to_dict()
        assert d["token_threshold"] == 10000
        assert d["enabled"] is True
        assert "preserve_recent_count" in d

    def test_from_dict(self):
        d = {"enabled": False, "token_threshold": 2000, "extra_key": "ignored"}
        c = CompressionConfig.from_dict(d)
        assert c.enabled is False
        assert c.token_threshold == 2000

    def test_roundtrip(self):
        c1 = CompressionConfig(token_threshold=999, preserve_recent_count=7)
        c2 = CompressionConfig.from_dict(c1.to_dict())
        assert c1 == c2

    def test_from_dict_ignores_unknown(self):
        c = CompressionConfig.from_dict({"foo": "bar"})
        assert c == CompressionConfig()


# ===========================================================================
# TokenEstimator
# ===========================================================================

class TestTokenEstimator:
    def test_estimate_empty(self):
        est = TokenEstimator()
        assert est.estimate([]) == 0

    def test_estimate_single_message(self):
        est = TokenEstimator()
        msg = {"content": "a" * 40}
        assert est.estimate_message(msg) == 10

    def test_estimate_min_one_token(self):
        est = TokenEstimator()
        msg = {"content": "a"}
        assert est.estimate_message(msg) == 1

    def test_estimate_multiple(self):
        est = TokenEstimator()
        msgs = [{"content": "a" * 20}, {"content": "b" * 40}]
        assert est.estimate(msgs) == 15

    def test_estimate_non_string_content(self):
        est = TokenEstimator()
        msg = {"content": 12345}
        assert est.estimate_message(msg) >= 1

    def test_estimate_missing_content(self):
        est = TokenEstimator()
        msg = {}
        assert est.estimate_message(msg) == 1

    def test_custom_token_func(self):
        est = TokenEstimator(token_func=lambda s: len(s.split()))
        msg = {"content": "one two three four"}
        assert est.estimate_message(msg) == 4

    def test_custom_token_func_min_one(self):
        est = TokenEstimator(token_func=lambda s: 0)
        msg = {"content": ""}
        assert est.estimate_message(msg) == 1


# ===========================================================================
# CompressionTriggerEvaluator
# ===========================================================================

class TestCompressionTriggerEvaluator:
    def test_below_threshold_no_pressure(self):
        ev = CompressionTriggerEvaluator(CompressionConfig(token_threshold=8000))
        msgs = [{"content": "x"}]
        assert ev.evaluate(100, msgs, 0) == CompressionTrigger.NONE

    def test_at_threshold(self):
        ev = CompressionTriggerEvaluator(CompressionConfig(token_threshold=8000))
        msgs = [{"content": "x"}] * 10
        assert ev.evaluate(8000, msgs, 0) == CompressionTrigger.TOKEN_THRESHOLD

    def test_above_threshold(self):
        ev = CompressionTriggerEvaluator(CompressionConfig(token_threshold=8000))
        msgs = [{"content": "x"}] * 10
        assert ev.evaluate(10000, msgs, 0) == CompressionTrigger.TOKEN_THRESHOLD

    def test_context_pressure(self):
        ev = CompressionTriggerEvaluator(CompressionConfig(token_threshold=8000))
        msgs = [{"content": "x"}] * 51
        assert ev.evaluate(100, msgs, 0) == CompressionTrigger.CONTEXT_PRESSURE

    def test_disabled_config(self):
        ev = CompressionTriggerEvaluator(CompressionConfig(enabled=False))
        msgs = [{"content": "x"}] * 100
        assert ev.evaluate(100000, msgs, 0) == CompressionTrigger.NONE

    def test_max_compressions_reached(self):
        ev = CompressionTriggerEvaluator(
            CompressionConfig(max_compressions_per_task=2)
        )
        msgs = [{"content": "x"}] * 10
        assert ev.evaluate(10000, msgs, 2) == CompressionTrigger.NONE

    def test_max_compressions_not_reached(self):
        ev = CompressionTriggerEvaluator(
            CompressionConfig(max_compressions_per_task=3)
        )
        msgs = [{"content": "x"}] * 10
        assert ev.evaluate(10000, msgs, 2) == CompressionTrigger.TOKEN_THRESHOLD

    def test_none_messages(self):
        ev = CompressionTriggerEvaluator(CompressionConfig(token_threshold=100))
        assert ev.evaluate(200, None, 0) == CompressionTrigger.TOKEN_THRESHOLD

    def test_empty_messages_below_threshold(self):
        ev = CompressionTriggerEvaluator(CompressionConfig(token_threshold=8000))
        assert ev.evaluate(100, [], 0) == CompressionTrigger.NONE

    def test_should_compress_true(self):
        assert CompressionTriggerEvaluator.should_compress(
            CompressionTrigger.TOKEN_THRESHOLD
        ) is True

    def test_should_compress_false(self):
        assert CompressionTriggerEvaluator.should_compress(
            CompressionTrigger.NONE
        ) is False

    def test_should_compress_manual(self):
        assert CompressionTriggerEvaluator.should_compress(
            CompressionTrigger.MANUAL
        ) is True


# ===========================================================================
# CompressionResult
# ===========================================================================

class TestCompressionResult:
    def test_basic(self):
        r = CompressionResult(
            original_token_count=1000,
            compressed_token_count=300,
            trigger="token_threshold",
            messages_preserved=5,
            messages_compressed=10,
        )
        assert r.tokens_saved == 700
        assert r.compression_ratio_achieved == 0.3

    def test_zero_original(self):
        r = CompressionResult(
            original_token_count=0,
            compressed_token_count=0,
            trigger="none",
            messages_preserved=0,
            messages_compressed=0,
        )
        assert r.tokens_saved == 0
        assert r.compression_ratio_achieved == 1.0

    def test_compressed_more_than_original(self):
        r = CompressionResult(
            original_token_count=100,
            compressed_token_count=200,
            trigger="none",
            messages_preserved=1,
            messages_compressed=0,
        )
        assert r.tokens_saved == 0

    def test_to_dict(self):
        r = CompressionResult(
            original_token_count=1000,
            compressed_token_count=300,
            trigger="token_threshold",
            messages_preserved=5,
            messages_compressed=10,
        )
        d = r.to_dict()
        assert d["original_token_count"] == 1000
        assert d["compressed_token_count"] == 300

    def test_from_dict(self):
        d = {
            "original_token_count": 500,
            "compressed_token_count": 200,
            "trigger": "context_pressure",
            "messages_preserved": 3,
            "messages_compressed": 7,
        }
        r = CompressionResult.from_dict(d)
        assert r.original_token_count == 500
        assert r.trigger == "context_pressure"

    def test_roundtrip(self):
        r1 = CompressionResult(1000, 300, "token_threshold", 5, 10)
        r2 = CompressionResult.from_dict(r1.to_dict())
        assert r1 == r2

    def test_metadata_default_empty(self):
        r = CompressionResult(100, 50, "manual", 1, 1)
        assert r.metadata == {}


# ===========================================================================
# KnowledgeBlockExtractor
# ===========================================================================

class TestKnowledgeBlockExtractor:
    def test_extract_empty(self):
        ext = KnowledgeBlockExtractor()
        assert ext.extract([]) == []

    def test_extract_decision(self):
        ext = KnowledgeBlockExtractor()
        msgs = [{"role": "assistant", "content": "I decided to use approach A."}]
        blocks = ext.extract(msgs)
        assert len(blocks) == 1
        assert "decision" in blocks[0]["labels"]
        assert blocks[0]["source_index"] == 0

    def test_extract_file_path(self):
        ext = KnowledgeBlockExtractor()
        msgs = [{"role": "tool", "content": "Read file /home/user/config.yaml"}]
        blocks = ext.extract(msgs)
        assert len(blocks) >= 1
        assert any("file_path" in b["labels"] for b in blocks)

    def test_extract_error(self):
        ext = KnowledgeBlockExtractor()
        msgs = [{"role": "tool", "content": "Error: file not found"}]
        blocks = ext.extract(msgs)
        assert len(blocks) == 1
        assert "error" in blocks[0]["labels"]

    def test_extract_no_knowledge(self):
        ext = KnowledgeBlockExtractor()
        msgs = [{"role": "user", "content": "hello"}]
        assert ext.extract(msgs) == []

    def test_extract_from_message_none_content(self):
        ext = KnowledgeBlockExtractor()
        assert ext.extract_from_message({"content": None}) is None

    def test_extract_from_message_empty(self):
        ext = KnowledgeBlockExtractor()
        assert ext.extract_from_message({"content": ""}) is None

    def test_extract_from_message_missing_content(self):
        ext = KnowledgeBlockExtractor()
        assert ext.extract_from_message({}) is None

    def test_extract_from_message_non_string(self):
        ext = KnowledgeBlockExtractor()
        assert ext.extract_from_message({"content": 123}) is None

    def test_multiple_messages_mixed(self):
        ext = KnowledgeBlockExtractor()
        msgs = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "I chose method B"},
            {"role": "user", "content": "ok"},
            {"role": "tool", "content": "Error: timeout"},
        ]
        blocks = ext.extract(msgs)
        assert len(blocks) == 2
        assert blocks[0]["source_index"] == 1
        assert blocks[1]["source_index"] == 3

    def test_content_capped(self):
        ext = KnowledgeBlockExtractor()
        long_content = "decided " + "x" * 1000
        msg = {"role": "assistant", "content": long_content}
        block = ext.extract_from_message(msg)
        assert block is not None
        assert len(block["content"]) <= 500

    def test_source_indices(self):
        ext = KnowledgeBlockExtractor()
        msgs = [
            {"role": "a", "content": "no knowledge"},
            {"role": "b", "content": "decided to go"},
            {"role": "c", "content": "nothing here"},
            {"role": "d", "content": "error occurred"},
        ]
        blocks = ext.extract(msgs)
        assert [b["source_index"] for b in blocks] == [1, 3]


# ===========================================================================
# IntraTaskCompressor
# ===========================================================================

class TestIntraTaskCompressor:
    def _make_msgs(self, n: int, content_size: int = 20) -> list:
        return [{"role": "assistant", "content": "x" * content_size} for _ in range(n)]

    def test_compress_empty(self):
        comp = IntraTaskCompressor(CompressionConfig())
        new_msgs, result = comp.compress([])
        assert new_msgs == []
        assert result.original_token_count == 0
        assert result.messages_compressed == 0

    def test_compress_below_preserve_count(self):
        comp = IntraTaskCompressor(CompressionConfig(preserve_recent_count=5))
        msgs = self._make_msgs(3)
        new_msgs, result = comp.compress(msgs)
        assert len(new_msgs) == 3
        assert result.messages_compressed == 0

    def test_compress_exact_preserve_count(self):
        comp = IntraTaskCompressor(CompressionConfig(preserve_recent_count=5))
        msgs = self._make_msgs(5)
        new_msgs, result = comp.compress(msgs)
        assert len(new_msgs) == 5
        assert result.messages_compressed == 0

    def test_compress_above_preserve_count(self):
        comp = IntraTaskCompressor(CompressionConfig(preserve_recent_count=3))
        msgs = self._make_msgs(10, content_size=200)
        new_msgs, result = comp.compress(msgs)
        assert result.messages_compressed == 7
        assert result.messages_preserved == 3  # recent only
        assert len(new_msgs) <= len(msgs)  # should have shrunk
        assert result.tokens_saved > 0

    def test_compress_propagates_trigger(self):
        # Regression: the result echoes the trigger that fired compression,
        # not a hard-coded TOKEN_THRESHOLD.
        comp = IntraTaskCompressor(CompressionConfig(preserve_recent_count=3))
        msgs = self._make_msgs(10, content_size=200)
        _, result = comp.compress(msgs, trigger=CompressionTrigger.CONTEXT_PRESSURE)
        assert result.trigger == "context_pressure"

    def test_compression_ratio_affects_summary_size(self):
        # Regression: compression_ratio is honoured — a lower ratio yields a
        # smaller compressed footprint (previously the field was ignored).
        msgs = [{"role": "assistant", "content": "x" * 400} for _ in range(10)]
        _, r_low = IntraTaskCompressor(
            CompressionConfig(preserve_recent_count=3, compression_ratio=0.1)
        ).compress(list(msgs))
        _, r_high = IntraTaskCompressor(
            CompressionConfig(preserve_recent_count=3, compression_ratio=0.9)
        ).compress(list(msgs))
        assert r_low.compressed_token_count < r_high.compressed_token_count

    def test_system_messages_preserved(self):
        comp = IntraTaskCompressor(
            CompressionConfig(preserve_recent_count=2, preserve_system_messages=True)
        )
        msgs = [
            {"role": "system", "content": "system prompt"},
            {"role": "assistant", "content": "msg1"},
            {"role": "assistant", "content": "msg2"},
            {"role": "assistant", "content": "msg3"},
            {"role": "assistant", "content": "recent1"},
            {"role": "assistant", "content": "recent2"},
        ]
        new_msgs, result = comp.compress(msgs)
        roles = [m["role"] for m in new_msgs]
        assert roles.count("system") >= 2  # original + summary
        assert result.messages_preserved >= 3  # 2 recent + 1 system

    def test_system_messages_not_preserved_when_disabled(self):
        comp = IntraTaskCompressor(
            CompressionConfig(
                preserve_recent_count=2, preserve_system_messages=False
            )
        )
        msgs = [
            {"role": "system", "content": "system prompt"},
            {"role": "assistant", "content": "msg1"},
            {"role": "assistant", "content": "msg2"},
            {"role": "assistant", "content": "recent1"},
            {"role": "assistant", "content": "recent2"},
        ]
        new_msgs, result = comp.compress(msgs)
        # system message should have been compressed, not preserved
        assert result.messages_compressed == 3

    def test_compressed_message_has_marker(self):
        comp = IntraTaskCompressor(CompressionConfig(preserve_recent_count=2))
        msgs = self._make_msgs(10)
        new_msgs, _ = comp.compress(msgs)
        compressed_msgs = [m for m in new_msgs if m.get("_compressed")]
        assert len(compressed_msgs) == 1

    def test_compress_reduces_tokens(self):
        comp = IntraTaskCompressor(CompressionConfig(preserve_recent_count=3))
        msgs = self._make_msgs(20, content_size=100)
        new_msgs, result = comp.compress(msgs)
        assert result.compressed_token_count < result.original_token_count

    def test_compress_preserves_recent_content(self):
        comp = IntraTaskCompressor(CompressionConfig(preserve_recent_count=3))
        msgs = []
        for i in range(10):
            msgs.append({"role": "assistant", "content": f"unique_{i}"})
        new_msgs, result = comp.compress(msgs)
        contents = [m["content"] for m in new_msgs]
        assert "unique_7" in contents
        assert "unique_8" in contents
        assert "unique_9" in contents

    def test_stats_initial(self):
        comp = IntraTaskCompressor(CompressionConfig())
        stats = comp.get_stats()
        assert stats["total_compressions"] == 0
        assert stats["total_tokens_saved"] == 0

    def test_stats_after_compression(self):
        comp = IntraTaskCompressor(CompressionConfig(preserve_recent_count=3))
        msgs = self._make_msgs(20, content_size=100)
        comp.compress(msgs)
        stats = comp.get_stats()
        assert stats["total_compressions"] == 1
        assert stats["total_tokens_saved"] > 0
        assert 0 < stats["average_ratio"] < 1

    def test_stats_multiple_compressions(self):
        comp = IntraTaskCompressor(CompressionConfig(preserve_recent_count=3))
        for _ in range(3):
            comp.compress(self._make_msgs(20, content_size=100))
        stats = comp.get_stats()
        assert stats["total_compressions"] == 3

    def test_compressed_summary_includes_count(self):
        comp = IntraTaskCompressor(CompressionConfig(preserve_recent_count=2))
        msgs = self._make_msgs(10)
        new_msgs, _ = comp.compress(msgs)
        summary_msg = [m for m in new_msgs if m.get("_compressed")][0]
        # 10 total - 2 recent = 8 compressed
        assert "8" in summary_msg["content"]

    def test_compress_single_message(self):
        comp = IntraTaskCompressor(CompressionConfig(preserve_recent_count=5))
        msgs = [{"role": "user", "content": "hi"}]
        new_msgs, result = comp.compress(msgs)
        assert len(new_msgs) == 1
        assert result.messages_compressed == 0

    def test_compress_all_system_messages(self):
        comp = IntraTaskCompressor(
            CompressionConfig(preserve_recent_count=2, preserve_system_messages=True)
        )
        msgs = [
            {"role": "system", "content": f"sys {i}"}
            for i in range(10)
        ]
        new_msgs, result = comp.compress(msgs)
        # All preserved since they're system messages
        assert result.messages_compressed == 0
        assert result.messages_preserved == 10

    def test_compress_preserve_recent_zero(self):
        comp = IntraTaskCompressor(CompressionConfig(preserve_recent_count=0))
        msgs = self._make_msgs(5)
        new_msgs, result = comp.compress(msgs)
        assert result.messages_compressed == 5
        assert result.messages_preserved == 0

    def test_compress_preserve_recent_more_than_messages(self):
        comp = IntraTaskCompressor(CompressionConfig(preserve_recent_count=100))
        msgs = self._make_msgs(5)
        new_msgs, result = comp.compress(msgs)
        assert result.messages_compressed == 0
        assert len(new_msgs) == 5


# ===========================================================================
# Integration
# ===========================================================================

class TestIntegration:
    def test_full_flow_trigger_then_compress(self):
        config = CompressionConfig(token_threshold=100, preserve_recent_count=2)
        evaluator = CompressionTriggerEvaluator(config)
        estimator = TokenEstimator()
        comp = IntraTaskCompressor(config)

        msgs = self._make_msgs(20, content_size=50)  # ~250 tokens
        token_count = estimator.estimate(msgs)
        trigger = evaluator.evaluate(token_count, msgs, 0)

        assert trigger == CompressionTrigger.TOKEN_THRESHOLD
        assert CompressionTriggerEvaluator.should_compress(trigger)

        new_msgs, result = comp.compress(msgs)
        assert result.tokens_saved > 0
        assert len(new_msgs) < len(msgs)

    def test_full_flow_no_trigger(self):
        config = CompressionConfig(token_threshold=10000)
        evaluator = CompressionTriggerEvaluator(config)
        estimator = TokenEstimator()

        msgs = self._make_msgs(5, content_size=20)
        token_count = estimator.estimate(msgs)
        trigger = evaluator.evaluate(token_count, msgs, 0)

        assert trigger == CompressionTrigger.NONE
        assert not CompressionTriggerEvaluator.should_compress(trigger)

    @staticmethod
    def _make_msgs(n, content_size=20):
        return [{"role": "assistant", "content": "x" * content_size} for _ in range(n)]


# ===========================================================================
# Edge cases
# ===========================================================================

class TestEdgeCases:
    def test_none_messages_to_compressor(self):
        comp = IntraTaskCompressor(CompressionConfig())
        new_msgs, result = comp.compress([])
        assert new_msgs == []

    def test_very_large_message_content(self):
        comp = IntraTaskCompressor(CompressionConfig(preserve_recent_count=2))
        msgs = [
            {"role": "assistant", "content": "x" * 100000},
            {"role": "assistant", "content": "y" * 100000},
            {"role": "assistant", "content": "recent1"},
            {"role": "assistant", "content": "recent2"},
        ]
        new_msgs, result = comp.compress(msgs)
        assert result.compressed_token_count < result.original_token_count

    def test_unicode_content(self):
        comp = IntraTaskCompressor(CompressionConfig(preserve_recent_count=2))
        msgs = [
            {"role": "assistant", "content": "日本語テスト 🎉"},
            {"role": "assistant", "content": "another"},
            {"role": "assistant", "content": "recent1"},
            {"role": "assistant", "content": "recent2"},
        ]
        new_msgs, result = comp.compress(msgs)
        assert result.messages_compressed >= 1

    def test_metadata_contains_knowledge_count(self):
        comp = IntraTaskCompressor(CompressionConfig(preserve_recent_count=2))
        msgs = [
            {"role": "assistant", "content": "I decided to use approach A."},
            {"role": "assistant", "content": "Read /etc/config.yaml"},
            {"role": "assistant", "content": "recent1"},
            {"role": "assistant", "content": "recent2"},
        ]
        _, result = comp.compress(msgs)
        assert "knowledge_blocks" in result.metadata

    def test_multiple_compress_calls_accumulate_stats(self):
        comp = IntraTaskCompressor(CompressionConfig(preserve_recent_count=2))
        for _ in range(5):
            comp.compress(self._make_msgs(10, content_size=200))
        stats = comp.get_stats()
        assert stats["total_compressions"] == 5
        assert stats["total_tokens_saved"] > 0

    @staticmethod
    def _make_msgs(n, content_size=20):
        return [{"role": "assistant", "content": "x" * content_size} for _ in range(n)]
