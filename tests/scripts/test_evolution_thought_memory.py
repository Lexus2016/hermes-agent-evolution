# -*- coding: utf-8 -*-
"""Tests for the versioned thought-memory store (issue #2900, Thought-Retriever)."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from evolution_thought_memory import (  # noqa: E402
    MAX_STORE_ENTRIES,
    MIN_THOUGHT_CHARS,
    Thought,
    capture_thought,
    load_thoughts,
    main,
    normalize_thought,
    retrieve_thoughts,
    stats,
    thought_dedup_key,
)


class TestCapture:
    def test_captures_and_persists(self, tmp_path):
        t = capture_thought(
            "The promotion gate needs reasoning evidence, not just outcomes.",
            task_key="tk-1",
            store_dir=tmp_path,
        )
        assert t is not None
        assert t.version == 1
        assert t.task_key == "tk-1"
        loaded = load_thoughts(tmp_path)
        assert len(loaded) == 1
        assert loaded[0].text == t.text

    def test_versions_increment(self, tmp_path):
        for expected in (1, 2):
            t = capture_thought(f"distinct thought number {expected} here", store_dir=tmp_path)
            assert t.version == expected

    def test_filters_meaningless_short_thoughts(self, tmp_path):
        short = "x" * (MIN_THOUGHT_CHARS - 1)
        assert capture_thought(short, store_dir=tmp_path) is None
        assert load_thoughts(tmp_path) == []

    def test_filters_oversized_thoughts(self, tmp_path):
        big = "y" * 5000
        assert capture_thought(big, store_dir=tmp_path) is None

    def test_dedups_identical_thoughts(self, tmp_path):
        text = "The same reasoning fragment captured twice must be stored once."
        first = capture_thought(text, store_dir=tmp_path)
        second = capture_thought(text, store_dir=tmp_path)
        assert first is not None
        assert second is None
        assert len(load_thoughts(tmp_path)) == 1

    def test_dedup_ignores_whitespace_variance(self, tmp_path):
        a = capture_thought("split   across   many   spaces", store_dir=tmp_path)
        b = capture_thought("split across many spaces", store_dir=tmp_path)
        assert a is not None
        assert b is None


class TestRetrieve:
    def _seed(self, tmp_path):
        capture_thought("The agent should prefer tools over raw shell.", store_dir=tmp_path)
        capture_thought("Quantization shrinks decision margins on tool calls.", store_dir=tmp_path)
        capture_thought("Skill retrieval is the bottleneck as pools grow.", store_dir=tmp_path)

    def test_returns_top_k_by_token_overlap(self, tmp_path):
        self._seed(tmp_path)
        hits = retrieve_thoughts("tool calls and shell", k=2, store_dir=tmp_path)
        assert len(hits) <= 2
        # Both tool-related thoughts must outrank the unrelated skill thought.
        joined = " ".join(h.text.lower() for h in hits)
        assert "tool" in joined
        assert "skill retrieval" not in joined

    def test_empty_query_returns_nothing(self, tmp_path):
        self._seed(tmp_path)
        assert retrieve_thoughts("", store_dir=tmp_path) == []

    def test_no_overlap_returns_nothing(self, tmp_path):
        self._seed(tmp_path)
        assert retrieve_thoughts("zzzz qqqq", store_dir=tmp_path) == []


class TestStatsAndOverflow:
    def test_stats_counts_and_sources(self, tmp_path):
        capture_thought("first meaningful thought for stats", store_dir=tmp_path)
        capture_thought(
            "second meaningful thought from the gate",
            source="promotion-gate",
            store_dir=tmp_path,
        )
        s = stats(tmp_path)
        assert s["count"] == 2
        assert "task-cycle" in s["sources"]
        assert "promotion-gate" in s["sources"]

    def test_store_bounded_but_archive_keeps_history(self, tmp_path):
        for i in range(MAX_STORE_ENTRIES + 10):
            capture_thought(f"thought number {i} with a bit of padding text", store_dir=tmp_path)
        assert len(load_thoughts(tmp_path)) <= MAX_STORE_ENTRIES
        archive = tmp_path / "thoughts-archive.jsonl"
        assert archive.exists()
        assert archive.stat().st_size > 0


class TestNormalize:
    def test_normalize_collapses_whitespace(self):
        assert normalize_thought("  a   b\n c  ") == "a b c"

    def test_dedup_key_stable(self):
        assert thought_dedup_key("a b") == thought_dedup_key("a   b")


class TestCli:
    def test_capture_then_stats(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("EVOLUTION_PROFILE_DIR", str(tmp_path))
        assert main(["capture", "A thought worth keeping for the gate."]) == 0
        capsys.readouterr()
        assert main(["stats"]) == 0
        assert json.loads(capsys.readouterr().out)["count"] == 1

    def test_retrieve_cli(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("EVOLUTION_PROFILE_DIR", str(tmp_path))
        main(["capture", "Tool calls carry the decision signal clearly."])
        capsys.readouterr()
        assert main(["retrieve", "tool calls"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert len(out) >= 1

    def test_retrieve_no_hits_exits_one(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("EVOLUTION_PROFILE_DIR", str(tmp_path))
        assert main(["retrieve", "zzzz"]) == 1
        capsys.readouterr()

    def test_bad_command_exits_two(self, capsys):
        assert main(["nonsense"]) == 2
        capsys.readouterr()
