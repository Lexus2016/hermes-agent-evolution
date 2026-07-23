"""Tests for the tool-memory store module (issue #1218)."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


@pytest.fixture
def tm_env(tmp_path, monkeypatch):
    """Isolated tool-memory environment with temp HERMES_HOME."""
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    return hermes_home


class TestToolMemoryStore:
    """Test ToolMemoryStore library API."""

    def test_add_and_get(self, tm_env):
        from scripts.evolution_tool_memory_store import ToolMemoryStore

        store = ToolMemoryStore()
        record = store.add(
            "terminal",
            capability="Execute shell commands",
            failure_boundaries=["No interactive prompts"],
            composition_partners=["read_file"],
        )
        assert record["tool"] == "terminal"
        assert record["capability"] == "Execute shell commands"
        assert "last_verified" in record

        retrieved = store.get("terminal")
        assert retrieved is not None
        assert retrieved["capability"] == "Execute shell commands"
        assert retrieved["failure_boundaries"] == ["No interactive prompts"]

    def test_add_updates_existing(self, tm_env):
        from scripts.evolution_tool_memory_store import ToolMemoryStore

        store = ToolMemoryStore()
        store.add("terminal", capability="Run commands")
        store.add(
            "terminal",
            capability="Execute shell commands",
            failure_boundaries=["No PTY"],
        )

        record = store.get("terminal")
        assert record["capability"] == "Execute shell commands"
        assert record["failure_boundaries"] == ["No PTY"]

    def test_list_all(self, tm_env):
        from scripts.evolution_tool_memory_store import ToolMemoryStore

        store = ToolMemoryStore()
        store.add("terminal", capability="Shell")
        store.add("read_file", capability="Read files")
        store.add("web_search", capability="Search web")

        records = store.list_all()
        assert len(records) == 3
        assert records[0]["tool"] == "read_file"  # sorted alphabetically

    def test_remove(self, tm_env):
        from scripts.evolution_tool_memory_store import ToolMemoryStore

        store = ToolMemoryStore()
        store.add("terminal", capability="Shell")
        assert store.remove("terminal") is True
        assert store.get("terminal") is None
        assert store.remove("terminal") is False

    def test_query_by_capability(self, tm_env):
        from scripts.evolution_tool_memory_store import ToolMemoryStore

        store = ToolMemoryStore()
        store.add("terminal", capability="Execute shell commands")
        store.add("web_search", capability="Search the web for information")
        store.add("read_file", capability="Read file contents")

        results = store.query(capability_keyword="search")
        assert len(results) == 1
        assert results[0]["tool"] == "web_search"

    def test_query_empty_returns_all(self, tm_env):
        from scripts.evolution_tool_memory_store import ToolMemoryStore

        store = ToolMemoryStore()
        store.add("terminal", capability="Shell")
        store.add("read_file", capability="Read")
        results = store.query()
        assert len(results) == 2

    def test_get_nonexistent_returns_none(self, tm_env):
        from scripts.evolution_tool_memory_store import ToolMemoryStore

        store = ToolMemoryStore()
        assert store.get("nonexistent") is None


class TestToolMemoryStoreCLI:
    """Test the CLI entry point."""

    def test_cli_add_and_list(self, tm_env):
        from scripts.evolution_tool_memory_store import main

        rc = main([
            "add",
            "--tool",
            "terminal",
            "--capability",
            "Execute shell commands",
            "--failure-boundary",
            "No interactive prompts",
        ])
        assert rc == 0

        rc = main(["list"])
        assert rc == 0

    def test_cli_query_by_tool(self, tm_env):
        from scripts.evolution_tool_memory_store import main

        main(["add", "--tool", "terminal", "--capability", "Shell"])
        rc = main(["query", "--tool", "terminal"])
        assert rc == 0

    def test_cli_query_nonexistent(self, tm_env, capsys):
        from scripts.evolution_tool_memory_store import main

        rc = main(["query", "--tool", "nonexistent"])
        assert rc == 1

    def test_cli_remove(self, tm_env):
        from scripts.evolution_tool_memory_store import main

        main(["add", "--tool", "terminal", "--capability", "Shell"])
        rc = main(["remove", "--tool", "terminal"])
        assert rc == 0
        rc = main(["query", "--tool", "terminal"])
        assert rc == 1
