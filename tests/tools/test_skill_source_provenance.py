"""Tests for tools/skill_source_provenance.py — source-chain provenance (#2192).

Tests cover:
  - classify_source: trusted vs untrusted classification
  - ProvenanceEntry serialization
  - ContextVar chain accumulation + reset
  - record_skill_provenance persistence to sidecar
  - get_skill_provenance retrieval
"""

from unittest.mock import patch

import pytest

from tools.skill_source_provenance import (
    ProvenanceEntry,
    add_provenance_entry,
    get_provenance_chain,
    reset_provenance_chain,
    classify_source,
    record_skill_provenance,
    get_skill_provenance,
)


class TestClassifySource:
    def test_http_url_untrusted(self):
        assert classify_source("url", "https://evil.com/payload") is False

    def test_arxiv_trusted(self):
        assert classify_source("url", "https://arxiv.org/abs/2608.05810") is True

    def test_github_trusted(self):
        assert classify_source("url", "https://github.com/repo/code") is True

    def test_web_extract_tool_untrusted(self):
        assert classify_source("tool_call", "web_extract") is False

    def test_terminal_tool_trusted(self):
        assert classify_source("tool_call", "terminal") is True

    def test_read_file_trusted(self):
        assert classify_source("tool_call", "read_file") is True

    def test_unknown_tool_untrusted(self):
        assert classify_source("tool_call", "mystery_tool") is False

    def test_subagent_trusted(self):
        assert classify_source("subagent", "subagent-abc123") is True

    def test_file_trusted(self):
        assert classify_source("file", "/config/data.txt") is True

    def test_explicit_override(self):
        """add_provenance_entry trusts explicit trusted param over classification."""
        reset_provenance_chain()
        add_provenance_entry("url", "https://evil.com", trusted=True)
        chain = get_provenance_chain()
        assert chain[0].trusted is True


class TestProvenanceEntry:
    def test_serialization(self):
        e = ProvenanceEntry(source_type="url", source_id="https://x.com", trusted=False)
        d = e.to_dict()
        assert d["source_type"] == "url"
        assert d["trusted"] is False

        restored = ProvenanceEntry.from_dict(d)
        assert restored.source_type == "url"
        assert restored.source_id == "https://x.com"
        assert restored.trusted is False

    def test_default_trusted(self):
        e = ProvenanceEntry(source_type="file", source_id="/tmp/x")
        assert e.trusted is True


class TestProvenanceChain:
    def setup_method(self):
        reset_provenance_chain()

    def teardown_method(self):
        reset_provenance_chain()

    def test_accumulation(self):
        add_provenance_entry("tool_call", "read_file")
        add_provenance_entry("url", "https://evil.com")
        chain = get_provenance_chain()
        assert len(chain) == 2
        assert chain[0].source_id == "read_file"
        assert chain[1].source_id == "https://evil.com"

    def test_reset(self):
        add_provenance_entry("tool_call", "terminal")
        assert len(get_provenance_chain()) == 1
        reset_provenance_chain()
        assert len(get_provenance_chain()) == 0

    def test_auto_classification(self):
        add_provenance_entry("url", "https://random-site.com")
        chain = get_provenance_chain()
        assert chain[0].trusted is False

    def test_mixed_chain(self):
        add_provenance_entry("tool_call", "terminal")
        add_provenance_entry("url", "https://evil.com")
        add_provenance_entry("url", "https://arxiv.org/abs/1234")
        chain = get_provenance_chain()
        assert chain[0].trusted is True  # terminal
        assert chain[1].trusted is False  # evil.com
        assert chain[2].trusted is True  # arxiv


class TestRecordSkillProvenance:
    def test_persists_chain_and_resets(self):
        reset_provenance_chain()
        add_provenance_entry("tool_call", "terminal")
        add_provenance_entry("url", "https://evil.com")

        state = {}

        def fake_mutate(name, mutator, **kw):
            mutator(state)

        with patch("tools.skill_usage._mutate", side_effect=fake_mutate):
            record_skill_provenance("test-skill")

        assert "provenance_chain" in state
        assert len(state["provenance_chain"]) == 2
        assert state["provenance_has_untrusted"] is True
        assert "provenance_recorded_at" in state
        # Chain should be reset after recording
        assert len(get_provenance_chain()) == 0

    def test_noop_on_empty_chain(self):
        reset_provenance_chain()
        state = {}

        def fake_mutate(name, mutator, **kw):
            mutator(state)

        with patch("tools.skill_usage._mutate", side_effect=fake_mutate):
            record_skill_provenance("test")
        assert state == {}  # nothing recorded

    def test_all_trusted_chain(self):
        reset_provenance_chain()
        add_provenance_entry("tool_call", "terminal")

        state = {}

        def fake_mutate(name, mutator, **kw):
            mutator(state)

        with patch("tools.skill_usage._mutate", side_effect=fake_mutate):
            record_skill_provenance("safe-skill")
        assert state["provenance_has_untrusted"] is False


class TestGetSkillProvenance:
    def test_returns_summary(self):
        mock_rec = {
            "provenance_chain": [
                {"source_type": "tool_call", "source_id": "terminal", "trusted": True},
                {
                    "source_type": "url",
                    "source_id": "https://evil.com",
                    "trusted": False,
                },
            ],
            "provenance_has_untrusted": True,
            "provenance_recorded_at": "2026-08-10T00:00:00Z",
        }
        with patch("tools.skill_usage.get_record", return_value=mock_rec):
            result = get_skill_provenance("test")
        assert result["source_count"] == 2
        assert result["untrusted_count"] == 1
        assert result["has_untrusted"] is True

    def test_empty_on_missing(self):
        with patch("tools.skill_usage.get_record", return_value={}):
            result = get_skill_provenance("nonexistent")
        assert result["source_count"] == 0
        assert result["has_untrusted"] is False

    def test_error_safe(self):
        with patch("tools.skill_usage.get_record", side_effect=RuntimeError):
            result = get_skill_provenance("error")
        assert result["source_count"] == 0
