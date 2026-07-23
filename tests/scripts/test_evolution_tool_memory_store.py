"""Tests for the tool-memory store (#1218)."""

import json
from pathlib import Path

import pytest

from scripts.evolution_tool_memory_store import ToolMemoryStore


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Redirect HERMES_HOME so the store lands in a temp dir."""
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    return ToolMemoryStore()


class TestToolMemoryStore:
    """Core CRUD + query operations on the tool-memory store."""

    def test_put_and_get(self, store):
        record = {
            "tool_name": "terminal",
            "capability": "Execute shell commands",
            "failure_boundaries": ["no GUI", "no sudo"],
        }
        store.put(record)
        result = store.get("terminal")
        assert result is not None
        assert result["tool_name"] == "terminal"
        assert result["capability"] == "Execute shell commands"
        assert "last_verified" in result

    def test_put_missing_required_fields_raises(self, store):
        with pytest.raises(ValueError, match="Missing required fields"):
            store.put({"tool_name": "terminal"})  # no capability

    def test_put_missing_tool_name_raises(self, store):
        with pytest.raises(ValueError, match="Missing required fields"):
            store.put({"capability": "something"})

    def test_get_nonexistent_returns_none(self, store):
        assert store.get("nonexistent") is None

    def test_put_updates_existing_record(self, store):
        store.put({"tool_name": "terminal", "capability": "Run commands"})
        store.put({
            "tool_name": "terminal",
            "capability": "Run commands v2",
            "notes": "updated",
        })
        result = store.get("terminal")
        assert result["capability"] == "Run commands v2"
        assert result["notes"] == "updated"

    def test_query_by_capability_keyword(self, store):
        store.put({"tool_name": "terminal", "capability": "Execute shell commands"})
        store.put({"tool_name": "web_search", "capability": "Search the web"})
        store.put({"tool_name": "read_file", "capability": "Read file contents"})
        results = store.query("search")
        assert len(results) == 1
        assert results[0]["tool_name"] == "web_search"

    def test_query_by_tool_name_keyword(self, store):
        store.put({"tool_name": "web_search", "capability": "Search"})
        results = store.query("web")
        assert len(results) == 1
        assert results[0]["tool_name"] == "web_search"

    def test_query_case_insensitive(self, store):
        store.put({"tool_name": "terminal", "capability": "Execute SHELL Commands"})
        results = store.query("shell")
        assert len(results) == 1

    def test_query_no_matches(self, store):
        store.put({"tool_name": "terminal", "capability": "Run commands"})
        assert store.query("nonexistent_keyword") == []

    def test_list_all(self, store):
        store.put({"tool_name": "terminal", "capability": "Run commands"})
        store.put({"tool_name": "read_file", "capability": "Read files"})
        all_records = store.list_all()
        assert len(all_records) == 2

    def test_remove_existing(self, store):
        store.put({"tool_name": "terminal", "capability": "Run commands"})
        assert store.remove("terminal") is True
        assert store.get("terminal") is None

    def test_remove_nonexistent(self, store):
        assert store.remove("nonexistent") is False

    def test_count(self, store):
        assert store.count() == 0
        store.put({"tool_name": "terminal", "capability": "Run commands"})
        assert store.count() == 1
        store.put({"tool_name": "read_file", "capability": "Read files"})
        assert store.count() == 2
        store.remove("terminal")
        assert store.count() == 1

    def test_persistence_across_instances(self, tmp_path, monkeypatch):
        """Records survive across ToolMemoryStore instances (same file)."""
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        s1 = ToolMemoryStore()
        s1.put({"tool_name": "terminal", "capability": "Run commands"})

        s2 = ToolMemoryStore()
        result = s2.get("terminal")
        assert result is not None
        assert result["capability"] == "Run commands"

    def test_auto_stamps_last_verified(self, store):
        store.put({"tool_name": "terminal", "capability": "Run commands"})
        record = store.get("terminal")
        assert "last_verified" in record
        # Should be a valid ISO timestamp
        from datetime import datetime

        datetime.fromisoformat(record["last_verified"])

    def test_preserves_existing_last_verified_on_update(self, store):
        store.put({
            "tool_name": "terminal",
            "capability": "v1",
            "last_verified": "2020-01-01T00:00:00+00:00",
        })
        store.put({"tool_name": "terminal", "capability": "v2"})
        record = store.get("terminal")
        assert record["last_verified"] == "2020-01-01T00:00:00+00:00"
        assert record["capability"] == "v2"

    def test_composition_partners_preserved(self, store):
        store.put({
            "tool_name": "terminal",
            "capability": "Run commands",
            "composition_partners": ["read_file", "write_file", "patch"],
        })
        record = store.get("terminal")
        assert record["composition_partners"] == ["read_file", "write_file", "patch"]
