# -*- coding: utf-8 -*-
"""Unit tests for Programmatic Skills and SpeedRunner synthesizer (#2384)."""

from pathlib import Path
import pytest

from evolution.lib.programmatic_skills import (
    ProgrammaticSkill,
    ProgrammaticSkillLibrary,
    ProgrammaticSkillSynthesizer,
)


class TestProgrammaticSkills:
    """Test suite for programmatic skills creation, execution, and cost savings."""

    def test_programmatic_skill_serialization(self):
        skill = ProgrammaticSkill(
            name="clean_keys",
            description="Clean dictionary string values",
            code="def clean_keys(data):\n    return {k: str(v).strip() for k, v in data.items()}",
            entry_point="clean_keys",
            call_count=5,
            token_savings_estimate=2250,
        )
        d = skill.to_dict()
        assert d["name"] == "clean_keys"
        assert d["call_count"] == 5

        restored = ProgrammaticSkill.from_dict(d)
        assert restored.name == skill.name
        assert restored.token_savings_estimate == 2250

    def test_code_safety_validation(self):
        safe_code = "def parse_nums(s):\n    return [int(x) for x in s.split(',')]"
        assert ProgrammaticSkillSynthesizer.validate_code_safety(safe_code) is True

        unsafe_code_import = "import subprocess\ndef hack(): subprocess.run('ls')"
        assert (
            ProgrammaticSkillSynthesizer.validate_code_safety(unsafe_code_import)
            is False
        )

        unsafe_code_from = "from ctypes import c_int\ndef hack(): pass"
        assert (
            ProgrammaticSkillSynthesizer.validate_code_safety(unsafe_code_from) is False
        )

    def test_execute_skill_and_token_savings(self):
        code = """def extract_sum(numbers: list) -> int:
    return sum(numbers)
"""
        skill = ProgrammaticSkill(
            name="extract_sum",
            description="Sum numbers",
            code=code,
            entry_point="extract_sum",
        )
        res = ProgrammaticSkillSynthesizer.execute_skill(
            skill, {"numbers": [10, 20, 30]}
        )
        assert res["status"] == "success"
        assert res["output"] == 60
        assert res["token_savings"] == 450
        assert skill.call_count == 1
        assert skill.token_savings_estimate == 450

    def test_library_lifecycle(self, tmp_path: Path):
        lib = ProgrammaticSkillLibrary()
        skill = ProgrammaticSkill(
            name="format_headers",
            description="Title case dict headers",
            code="def format_headers(headers: dict) -> dict:\n    return {k.title(): v for k, v in headers.items()}",
            entry_point="format_headers",
        )
        lib.register(skill)

        res = lib.execute(
            "format_headers", {"headers": {"user-agent": "hermes", "accept": "*/*"}}
        )
        assert res["status"] == "success"
        assert res["output"] == {"User-Agent": "hermes", "Accept": "*/*"}
        assert lib.estimate_total_token_savings() == 450

        # Save and load
        save_path = tmp_path / "skills.json"
        lib.save_json(save_path)

        loaded = ProgrammaticSkillLibrary.load_json(save_path)
        assert loaded.get("format_headers") is not None
        assert loaded.estimate_total_token_savings() == 450
