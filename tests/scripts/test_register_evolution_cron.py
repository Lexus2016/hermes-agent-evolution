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


class TestNoAgentJobs:
    """no_agent yaml jobs (e.g. the watchdog) register as script-only cron
    jobs: the script is installed into HERMES_HOME/scripts, no LLM agent and
    no access-gate pre-check are attached."""

    def _write_watchdog_yaml(self, src_dir):
        (src_dir / "watchdog.yaml").write_text(
            "name: evolution-watchdog\n"
            'schedule: "47 7 * * *"\n'
            "enabled: true\n"
            "no_agent: true\n"
            "script: evolution_watchdog.py\n"
            "deliver: all\n"
            'prompt: "health check"\n'
        )

    def test_no_agent_job_registered_with_script(self, tmp_path, monkeypatch):
        mod = _import_module()
        src_dir = tmp_path / "cron-src"
        src_dir.mkdir()
        self._write_watchdog_yaml(src_dir)
        home = tmp_path / "hermes-home"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))

        captured = {}

        def fake_create_job(**kwargs):
            captured.update(kwargs)
            return {"id": "wd123", "name": kwargs["name"]}

        import cron.jobs as jobs_mod

        monkeypatch.setattr(jobs_mod, "create_job", fake_create_job)
        monkeypatch.setattr(jobs_mod, "load_jobs", lambda: [])

        rc = mod.main(["register_evolution_cron.py", str(src_dir)])

        assert rc == 0
        assert captured["no_agent"] is True
        assert captured["script"] == "evolution_watchdog.py"
        assert captured["deliver"] == "all"
        # The real watchdog script must have been installed into HERMES_HOME.
        assert (home / "scripts" / "evolution_watchdog.py").is_file()
        # No gate / skills / toolsets attached to a script-only job.
        assert "skills" not in captured
        assert "enabled_toolsets" not in captured

    def test_no_agent_without_script_fails(self, tmp_path, monkeypatch):
        mod = _import_module()
        src_dir = tmp_path / "cron-src"
        src_dir.mkdir()
        (src_dir / "bad.yaml").write_text(
            "name: evolution-bad\n"
            'schedule: "0 9 * * *"\n'
            "no_agent: true\n"
            'prompt: "x"\n'
        )
        home = tmp_path / "hermes-home"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))

        import cron.jobs as jobs_mod

        monkeypatch.setattr(jobs_mod, "load_jobs", lambda: [])

        rc = mod.main(["register_evolution_cron.py", str(src_dir)])
        assert rc == 2  # registration failure reported
