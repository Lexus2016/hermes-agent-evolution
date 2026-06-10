"""Tests for scripts.register_evolution_cron."""

import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "register_evolution_cron.py"


def _import_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("register_evolution_cron", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["register_evolution_cron"] = mod
    spec.loader.exec_module(mod)
    return mod


class TestNormalizeToolsets:
    def test_empty_returns_none(self):
        mod = _import_module()
        assert mod._normalize_toolsets(None) is None
        assert mod._normalize_toolsets([]) is None
        assert mod._normalize_toolsets("") is None

    def test_single_string_expanded(self):
        mod = _import_module()
        assert mod._normalize_toolsets("web") == ["web", "delegation"]

    def test_list_appends_delegation(self):
        mod = _import_module()
        assert mod._normalize_toolsets(["web", "file"]) == ["web", "file", "delegation"]

    def test_no_duplicate_delegation(self):
        mod = _import_module()
        assert mod._normalize_toolsets(["web", "delegation"]) == ["web", "delegation"]

    def test_blanks_dropped(self):
        mod = _import_module()
        assert mod._normalize_toolsets(["web", "", "file"]) == [
            "web",
            "file",
            "delegation",
        ]


class TestNormalizeSkills:
    def test_none_returns_none(self):
        mod = _import_module()
        assert mod._normalize_skills(None) is None

    def test_slash_replaced(self):
        mod = _import_module()
        assert mod._normalize_skills("evolution/research") == ["evolution-research"]

    def test_list_normalized(self):
        mod = _import_module()
        assert mod._normalize_skills(["evolution/research", "evolution/analysis"]) == [
            "evolution-research",
            "evolution-analysis",
        ]
