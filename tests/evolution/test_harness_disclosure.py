# -*- coding: utf-8 -*-
"""Unit tests for harness configuration disclosure (#2481)."""

import json
from pathlib import Path

from evolution.lib.harness_disclosure import (
    HarnessConfigSnapshot,
    capture_harness_config,
    write_harness_config,
)


class TestHarnessConfigSnapshot:
    def test_capture_populates_fields(self):
        snap = capture_harness_config(
            system_prompt_version="v1.2.3",
            model_provider="openai",
            model="gpt-4o",
            tool_names=["terminal", "read_file", "web_search"],
            context_management={"compression": "enabled", "max_tokens": 128000},
        )
        assert snap.system_prompt_version == "v1.2.3"
        assert snap.model_provider == "openai"
        assert snap.model == "gpt-4o"
        assert snap.tool_definitions == ["read_file", "terminal", "web_search"]
        assert snap.context_management["compression"] == "enabled"

    def test_capture_reads_skill_versions(self, tmp_path: Path):
        skill_dir = tmp_path / "skills" / "my-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            '---\nname: my-skill\nversion: "2.0.0"\n---\n# My Skill\n',
            encoding="utf-8",
        )
        snap = capture_harness_config(skills_dir=tmp_path / "skills")
        assert snap.skill_versions.get("my-skill") == "2.0.0"

    def test_capture_missing_fields_are_empty(self):
        snap = capture_harness_config()
        assert snap.system_prompt_version == ""
        assert snap.model_provider == ""
        assert snap.model == ""
        assert snap.tool_definitions == []
        assert snap.skill_versions == {}

    def test_snapshot_serialization(self):
        snap = HarnessConfigSnapshot(
            system_prompt_version="v1",
            skill_versions={"a": "1.0"},
            tool_definitions=["t"],
            model_provider="p",
            model="m",
            context_management={"k": "v"},
        )
        d = snap.to_dict()
        restored = HarnessConfigSnapshot.from_dict(d)
        assert restored.system_prompt_version == "v1"
        assert restored.skill_versions == {"a": "1.0"}
        assert restored.model == "m"


class TestWriteHarnessConfig:
    def test_write_and_read_back(self, tmp_path: Path):
        snap = capture_harness_config(
            system_prompt_version="v1.0",
            model_provider="anthropic",
            model="claude-4",
        )
        path = write_harness_config(snap, tmp_path / "harness.json")
        assert path.exists()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["model_provider"] == "anthropic"
        assert data["model"] == "claude-4"
        assert data["system_prompt_version"] == "v1.0"
