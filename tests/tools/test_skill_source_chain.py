"""Tests for skill source-chain provenance (#2192).

Verifies that:
- Source entries are recorded only inside an active source-chain context
- Sources are classified trusted vs untrusted correctly
- The source chain is persisted to / loaded from the sidecar JSON
- Unknown source types default to untrusted (fail-safe)
- Foreground contexts never accumulate source entries
"""

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from tools.skill_provenance import (
    BACKGROUND_REVIEW,
    _classify_source,
    _source_chain,
    get_source_chain,
    get_source_chain_summary,
    init_source_chain,
    is_background_review,
    load_source_chain,
    record_source,
    reset_current_write_origin,
    save_source_chain,
    set_current_write_origin,
)


def _source_chain_reset():
    """Reset the source-chain ContextVar to None (foreground state)."""
    _source_chain.set(None)


# ---------------------------------------------------------------------------
# Source classification
# ---------------------------------------------------------------------------


class TestSourceClassification:
    def test_trusted_sources(self):
        assert _classify_source("terminal") is True
        assert _classify_source("read_file") is True
        assert _classify_source("search_files") is True
        assert _classify_source("execute_code") is True
        assert _classify_source("patch") is True
        assert _classify_source("write_file") is True
        assert _classify_source("delegate_task") is True

    def test_untrusted_sources(self):
        assert _classify_source("web_search") is False
        assert _classify_source("web_extract") is False
        assert _classify_source("browser_navigate") is False
        assert _classify_source("browser_action") is False
        assert _classify_source("external_tool") is False
        assert _classify_source("mcp_tool") is False

    def test_unknown_source_defaults_untrusted(self):
        """Fail-safe: unknown source types are untrusted."""
        assert _classify_source("some_new_tool") is False
        assert _classify_source("") is False
        assert _classify_source("unknown_thing") is False


# ---------------------------------------------------------------------------
# Source-chain accumulation (ContextVar)
# ---------------------------------------------------------------------------


class TestSourceChainAccumulation:
    def test_record_source_noop_without_init(self):
        """record_source is a no-op when no source-chain is initialized."""
        # Ensure no chain is active (reset to None like foreground context)
        _source_chain_reset()
        assert get_source_chain() == []
        record_source("terminal", source_ref="/tmp/test.py")
        assert get_source_chain() == []

    def test_record_source_with_init(self):
        """record_source appends to the chain when initialized."""
        _source_chain_reset()
        init_source_chain()
        record_source("terminal", source_ref="/tmp/test.py", detail="ls -la")
        record_source("web_search", source_ref="python best practices")
        chain = get_source_chain()
        assert len(chain) == 2
        assert chain[0]["source_type"] == "terminal"
        assert chain[0]["source_ref"] == "/tmp/test.py"
        assert chain[0]["trusted"] is True
        assert chain[1]["source_type"] == "web_search"
        assert chain[1]["source_ref"] == "python best practices"
        assert chain[1]["trusted"] is False

    def test_record_source_truncates_long_refs(self):
        """Source refs are truncated to 200 chars."""
        _source_chain_reset()
        init_source_chain()
        long_ref = "x" * 500
        record_source("read_file", source_ref=long_ref)
        chain = get_source_chain()
        assert len(chain[0]["source_ref"]) == 200

    def test_get_source_chain_returns_copy(self):
        """get_source_chain returns a copy, not the internal list."""
        _source_chain_reset()
        init_source_chain()
        record_source("terminal", source_ref="/tmp/a")
        chain1 = get_source_chain()
        chain1.append({"fake": True})
        chain2 = get_source_chain()
        assert len(chain2) == 1  # not affected by mutation of the copy


# ---------------------------------------------------------------------------
# Source-chain persistence (sidecar JSON)
# ---------------------------------------------------------------------------


class TestSourceChainPersistence:
    def test_save_and_load_source_chain(self, tmp_path, monkeypatch):
        """save_source_chain persists and load_source_chain retrieves."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        chain = [
            {"source_type": "terminal", "source_ref": "ls", "trusted": True},
            {"source_type": "web_search", "source_ref": "test", "trusted": False},
        ]
        save_source_chain("test-skill", chain)

        loaded = load_source_chain("test-skill")
        assert loaded is not None
        assert loaded["chain"] == chain
        assert "saved_at" in loaded

    def test_load_source_chain_missing_skill(self, tmp_path, monkeypatch):
        """load_source_chain returns None for a skill with no chain."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        assert load_source_chain("nonexistent") is None

    def test_save_empty_chain_is_noop(self, tmp_path, monkeypatch):
        """save_source_chain with an empty chain does nothing."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        save_source_chain("test-skill", [])
        # Sidecar file should not exist or be empty
        path = tmp_path / "skills" / ".source_chains.json"
        assert not path.exists()

    def test_get_source_chain_summary(self, tmp_path, monkeypatch):
        """get_source_chain_summary returns a readable summary."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        chain = [
            {"source_type": "terminal", "source_ref": "ls", "trusted": True},
            {"source_type": "read_file", "source_ref": "/tmp/a", "trusted": True},
            {"source_type": "web_search", "source_ref": "test", "trusted": False},
        ]
        save_source_chain("test-skill", chain)

        summary = get_source_chain_summary("test-skill")
        assert summary["has_source_chain"] is True
        assert summary["source_count"] == 3
        assert summary["trusted_sources"] == 2
        assert summary["untrusted_sources"] == 1
        assert summary["all_trusted"] is False

    def test_get_source_chain_summary_missing(self, tmp_path, monkeypatch):
        """get_source_chain_summary for a skill with no chain."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        summary = get_source_chain_summary("nonexistent")
        assert summary["has_source_chain"] is False

    def test_get_source_chain_summary_all_trusted(self, tmp_path, monkeypatch):
        """get_source_chain_summary with only trusted sources."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        chain = [
            {"source_type": "terminal", "source_ref": "ls", "trusted": True},
            {"source_type": "read_file", "source_ref": "/tmp/a", "trusted": True},
        ]
        save_source_chain("all-trusted-skill", chain)

        summary = get_source_chain_summary("all-trusted-skill")
        assert summary["all_trusted"] is True
        assert summary["untrusted_sources"] == 0


# ---------------------------------------------------------------------------
# Integration: background-review context
# ---------------------------------------------------------------------------


class TestBackgroundReviewIntegration:
    def test_source_chain_only_in_background_review(self):
        """Source chain only accumulates in background-review context."""
        # Foreground: no accumulation
        _source_chain_reset()
        assert not is_background_review()
        record_source("terminal", source_ref="test")
        assert get_source_chain() == []

        # Background review: accumulation works after init
        token = set_current_write_origin(BACKGROUND_REVIEW)
        try:
            assert is_background_review()
            init_source_chain()
            record_source("terminal", source_ref="/tmp/test.py")
            record_source("web_search", source_ref="python test")
            chain = get_source_chain()
            assert len(chain) == 2
            assert chain[0]["trusted"] is True
            assert chain[1]["trusted"] is False
        finally:
            _source_chain_reset()
            reset_current_write_origin(token)
