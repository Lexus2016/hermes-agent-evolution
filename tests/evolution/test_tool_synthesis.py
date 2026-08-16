# -*- coding: utf-8 -*-
"""Unit tests for the tool synthesis harness (#2259)."""

from pathlib import Path

from evolution.lib.tool_synthesis import (
    SandboxValidator,
    SynthesizedTool,
    ToolProposer,
    ToolRegistry,
)


class TestToolProposer:
    def test_propose_creates_tool(self):
        tool = ToolProposer.propose("parse a CSV file", "csv_parser")
        assert tool.name == "csv_parser"
        assert "parse a CSV file" in tool.description
        assert "def csv_parser" in tool.code
        assert tool.accepted is False

    def test_propose_sanitizes_name(self):
        tool = ToolProposer.propose("do a thing", "my-tool 2")
        assert tool.name == "my_tool_2"

    def test_tool_serialization(self):
        tool = SynthesizedTool(
            name="t", description="d", code="def t(): pass", accepted=True
        )
        d = tool.to_dict()
        restored = SynthesizedTool.from_dict(d)
        assert restored.name == "t"
        assert restored.accepted is True


class TestSandboxValidator:
    def test_valid_tool_succeeds(self):
        tool = ToolProposer.propose("parse a CSV file", "csv_parser")
        assert SandboxValidator.validate(tool, "a,b,c") is True

    def test_invalid_tool_fails(self):
        tool = SynthesizedTool(
            name="broken",
            description="broken tool",
            code="def broken(x):\n    raise RuntimeError('boom')\n",
        )
        assert SandboxValidator.validate(tool, "test") is False


class TestToolRegistry:
    def test_store_and_get(self, tmp_path: Path):
        registry = ToolRegistry(tmp_path / "registry.json")
        tool = ToolProposer.propose("parse a CSV file", "csv_parser")
        tool.accepted = True
        registry.store(tool)
        assert registry.list_names() == ["csv_parser"]
        restored = registry.get("csv_parser")
        assert restored is not None
        assert restored.name == "csv_parser"
        assert restored.accepted is True

    def test_get_missing_returns_none(self, tmp_path: Path):
        registry = ToolRegistry(tmp_path / "registry.json")
        assert registry.get("nope") is None

    def test_registry_persists_across_instances(self, tmp_path: Path):
        path = tmp_path / "registry.json"
        r1 = ToolRegistry(path)
        tool = ToolProposer.propose("do a thing", "thing_tool")
        r1.store(tool)
        r2 = ToolRegistry(path)
        assert r2.get("thing_tool") is not None
