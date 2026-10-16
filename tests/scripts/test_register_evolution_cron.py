"""Tests for scripts.register_evolution_cron."""

import json
import os
import subprocess
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
    # Existing tests exercise YAML reconcile, not the GitHub write gate.
    # Class-A jobs skip/pause when write is denied.
    mod._classify_write_access = lambda: "write"
    return mod


@pytest.fixture(autouse=True)
def _never_reexec(monkeypatch):
    """main() re-execs under the venv python when not already on it. In-process
    test calls of main() must NEVER re-exec — os.execv would replace the pytest
    process. The loop-guard env var disables it for every test here."""
    monkeypatch.setenv("_HERMES_REG_REEXEC", "1")


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
        monkeypatch.setattr(jobs_mod, "load_jobs", list)

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
            'name: evolution-bad\nschedule: "0 9 * * *"\nno_agent: true\nprompt: "x"\n'
        )
        home = tmp_path / "hermes-home"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))

        import cron.jobs as jobs_mod

        monkeypatch.setattr(jobs_mod, "load_jobs", list)

        rc = mod.main(["register_evolution_cron.py", str(src_dir)])
        assert rc == 2  # registration failure reported

    def test_existing_no_agent_job_still_refreshes_script(self, tmp_path, monkeypatch):
        """`hermes update` refreshes the repo, but the scheduler executes the
        copy in HERMES_HOME/scripts — re-running the registrar must refresh
        that copy even when the job itself is already registered."""
        mod = _import_module()
        src_dir = tmp_path / "cron-src"
        src_dir.mkdir()
        self._write_watchdog_yaml(src_dir)
        home = tmp_path / "hermes-home"
        (home / "scripts").mkdir(parents=True)
        # Stale installed copy from a previous registration.
        stale = home / "scripts" / "evolution_watchdog.py"
        stale.write_text("# stale old version\n")
        monkeypatch.setenv("HERMES_HOME", str(home))

        import cron.jobs as jobs_mod

        # Job already registered — create_job must NOT be called.
        monkeypatch.setattr(
            jobs_mod, "load_jobs", lambda: [{"name": "evolution-watchdog"}]
        )
        monkeypatch.setattr(
            jobs_mod,
            "create_job",
            lambda **kw: (_ for _ in ()).throw(AssertionError("must not create")),
        )

        rc = mod.main(["register_evolution_cron.py", str(src_dir)])

        assert rc == 0
        refreshed = stale.read_text()
        assert "stale old version" not in refreshed  # real script copied over

    def test_existing_no_agent_job_clears_legacy_inference_fields(
        self, tmp_path, monkeypatch
    ):
        mod = _import_module()
        src_dir = tmp_path / "cron-src"
        src_dir.mkdir()
        self._write_watchdog_yaml(src_dir)
        home = tmp_path / "hermes-home"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))

        import cron.jobs as jobs_mod

        schedule = jobs_mod.parse_schedule("47 7 * * *")
        existing = {
            "id": "watchdog-1",
            "name": "evolution-watchdog",
            "schedule": schedule,
            "schedule_display": schedule["display"],
            "no_agent": True,
            "script": "evolution_watchdog.py",
            "model": "legacy-model",
            "provider": "legacy-provider",
            "model_snapshot": "legacy-model",
            "provider_snapshot": "legacy-provider",
        }
        calls = []
        monkeypatch.setattr(jobs_mod, "load_jobs", lambda: [existing])
        monkeypatch.setattr(
            jobs_mod,
            "update_job",
            lambda job_id, updates: calls.append((job_id, updates)),
        )

        assert mod.main(["register_evolution_cron.py", str(src_dir)]) == 0
        assert calls == [
            (
                "watchdog-1",
                {
                    "model": None,
                    "provider": None,
                    "model_snapshot": None,
                    "provider_snapshot": None,
                },
            ),
            (
                "watchdog-1",
                {"model_snapshot": None, "provider_snapshot": None},
            ),
        ]


class TestReconcileExistingJob:
    """An edit to an already-registered evolution job's YAML must be applied via
    update_job — create_job is idempotent-by-name and would otherwise leave the
    old config in place (the historical re-register gotcha that froze schedule
    changes)."""

    def _write_agent_yaml(self, src_dir, schedule):
        (src_dir / "upstream-sync.yaml").write_text(
            "name: evolution-upstream-sync\n"
            f'schedule: "{schedule}"\n'
            "enabled: true\n"
            'prompt: "sync upstream"\n'
            "skills:\n"
            "  - evolution/upstream-sync\n"
            "toolsets:\n"
            "  - web\n"
            "  - file\n"
            "  - terminal\n"
        )

    def _existing(self, mod, jobs_mod, schedule):
        sched = jobs_mod.parse_schedule(schedule)
        return {
            "id": "job-123",
            "name": "evolution-upstream-sync",
            "schedule": sched,
            "schedule_display": sched.get("display"),
            "prompt": "sync upstream",
            "skills": mod._normalize_skills(["evolution/upstream-sync"]),
            "enabled_toolsets": mod._normalize_toolsets(["web", "file", "terminal"]),
            "provider_snapshot": "openrouter",
            "model_snapshot": "baseline-model",
        }

    def _wire(self, mod, jobs_mod, monkeypatch, tmp_path, existing):
        home = tmp_path / "hermes-home"
        (home / "scripts").mkdir(parents=True)
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setattr(jobs_mod, "load_jobs", lambda: [existing])
        monkeypatch.setattr(
            jobs_mod,
            "create_job",
            lambda **kw: (_ for _ in ()).throw(AssertionError("must not create")),
        )
        calls = {}
        monkeypatch.setattr(
            jobs_mod,
            "update_job",
            lambda job_id, updates: (
                calls.update(job_id=job_id, updates=updates) or {**existing, **updates}
            ),
        )
        return calls

    def test_changed_schedule_is_reconciled(self, tmp_path, monkeypatch):
        mod = _import_module()
        import cron.jobs as jobs_mod

        src_dir = tmp_path / "cron-src"
        src_dir.mkdir()
        self._write_agent_yaml(src_dir, "0 8 * * 1,3,5")  # new schedule in YAML
        existing = self._existing(mod, jobs_mod, "0 8 * * 0")  # old weekly schedule
        calls = self._wire(mod, jobs_mod, monkeypatch, tmp_path, existing)

        rc = mod.main(["register_evolution_cron.py", str(src_dir)])

        assert rc == 0
        assert calls["job_id"] == "job-123"
        assert calls["updates"]["schedule"] == "0 8 * * 1,3,5"
        assert calls["updates"]["model_snapshot"] is None
        assert calls["updates"]["provider_snapshot"] is None
        assert calls["updates"].get("script") == "evolution_access_gate.sh"

    def test_unchanged_job_clears_inference_snapshots(self, tmp_path, monkeypatch):
        mod = _import_module()
        import cron.jobs as jobs_mod

        src_dir = tmp_path / "cron-src"
        src_dir.mkdir()
        self._write_agent_yaml(src_dir, "0 8 * * 1,3,5")
        existing = self._existing(mod, jobs_mod, "0 8 * * 1,3,5")  # matches YAML
        calls = self._wire(mod, jobs_mod, monkeypatch, tmp_path, existing)

        rc = mod.main(["register_evolution_cron.py", str(src_dir)])

        assert rc == 0
        assert calls["updates"]["model_snapshot"] is None
        assert calls["updates"]["provider_snapshot"] is None
        assert calls["updates"].get("script") == "evolution_access_gate.sh"

    def test_unchanged_legacy_job_keeps_dynamic_inference(
        self, tmp_path, monkeypatch
    ):
        mod = _import_module()
        import cron.jobs as jobs_mod

        src_dir = tmp_path / "cron-src"
        src_dir.mkdir()
        self._write_agent_yaml(src_dir, "0 8 * * 1,3,5")
        existing = self._existing(mod, jobs_mod, "0 8 * * 1,3,5")
        existing.pop("provider_snapshot")
        existing.pop("model_snapshot")
        calls = self._wire(mod, jobs_mod, monkeypatch, tmp_path, existing)

        rc = mod.main(["register_evolution_cron.py", str(src_dir)])

        assert rc == 0
        # Class-A jobs without a YAML script get the write-access wake-gate
        # attached on reconcile; inference snapshots stay unset.
        assert calls["updates"] == {"script": "evolution_access_gate.sh"}

    def _write_agent_yaml_no_skills(self, src_dir, schedule):
        # An agent job whose YAML omits skills:/toolsets: entirely — the
        # _normalize_skills(None) -> None case that used to crash reconcile.
        (src_dir / "upstream-sync.yaml").write_text(
            "name: evolution-upstream-sync\n"
            f'schedule: "{schedule}"\n'
            "enabled: true\n"
            'prompt: "sync upstream"\n'
        )

    def _write_agent_yaml_with_script(self, src_dir, schedule, script_name):
        (src_dir / "upstream-sync.yaml").write_text(
            "name: evolution-upstream-sync\n"
            f'schedule: "{schedule}"\n'
            "enabled: true\n"
            'prompt: "sync upstream"\n'
            "skills:\n"
            "  - evolution/upstream-sync\n"
            "toolsets:\n"
            "  - web\n"
            "  - file\n"
            "  - terminal\n"
            f"script: {script_name}\n"
        )

    def test_changed_script_reconciles_record_and_installs_file(
        self, tmp_path, monkeypatch
    ):
        """CRITICAL regression (#910 review finding): reconciling an already-
        registered AGENT job's YAML-declared `script:` field (e.g. evolution-
        analysis moving from the generic evolution_access_gate.sh to its own
        evolution_analysis_gate.sh) must not just flip the DB record via
        update_job() — the new script file must actually be installed into
        HERMES_HOME/scripts/, or the scheduler ends up pointing at a file
        that was never copied and the wake-gate silently stops running."""
        mod = _import_module()
        import cron.jobs as jobs_mod

        src_dir = tmp_path / "cron-src"
        src_dir.mkdir()
        self._write_agent_yaml_with_script(
            src_dir, "0 8 * * 1,3,5", "evolution_analysis_gate.sh"
        )
        existing = self._existing(mod, jobs_mod, "0 8 * * 1,3,5")  # schedule matches
        existing["script"] = "evolution_access_gate.sh"  # stale script name
        calls = self._wire(mod, jobs_mod, monkeypatch, tmp_path, existing)

        rc = mod.main(["register_evolution_cron.py", str(src_dir)])

        assert rc == 0
        assert calls["updates"] == {
            "script": "evolution_analysis_gate.sh",
            "model_snapshot": None,
            "provider_snapshot": None,
        }
        home = tmp_path / "hermes-home"
        installed = home / "scripts" / "evolution_analysis_gate.sh"
        assert installed.is_file(), (
            "reconcile updated the job record but never installed the new "
            "script file into HERMES_HOME/scripts/"
        )

    def test_existing_agent_job_without_skills_does_not_crash(self, tmp_path, monkeypatch):
        """Regression: _normalize_skills(None) returns None; reconcile must not
        call list(None) — that TypeError silently aborted EVERY re-register (and
        thus every integration self-update) once the jobs already existed,
        freezing HERMES_HOME script/skill sync. A YAML that omits skills means
        'leave the registered skills as-is', never 'clear them'."""
        mod = _import_module()
        import cron.jobs as jobs_mod

        src_dir = tmp_path / "cron-src"
        src_dir.mkdir()
        # changed schedule (so update_job IS called), but no skills:/toolsets:
        self._write_agent_yaml_no_skills(src_dir, "0 8 * * 1,3,5")
        existing = self._existing(mod, jobs_mod, "0 8 * * 0")  # old schedule, HAS skills
        calls = self._wire(mod, jobs_mod, monkeypatch, tmp_path, existing)

        rc = mod.main(["register_evolution_cron.py", str(src_dir)])

        assert rc == 0  # did NOT crash on list(None)
        # Schedule reconciles; registered skills/toolsets stay (YAML omitted them).
        assert calls["updates"]["schedule"] == "0 8 * * 1,3,5"
        assert "skills" not in calls["updates"]
        assert "enabled_toolsets" not in calls["updates"]
        assert calls["updates"].get("script") == "evolution_access_gate.sh"


class TestDynamicInferenceRouting:
    """Evolution stages leave primary and fallback routing to AIAgent."""

    def test_rejects_per_stage_model_or_provider(self, tmp_path, monkeypatch):
        mod = _import_module()
        src_dir = tmp_path / "cron-src"
        src_dir.mkdir()
        (src_dir / "stage.yaml").write_text(
            "name: evolution-stage\n"
            'schedule: "0 9 * * *"\n'
            'prompt: "do the thing"\n'
            "model: forbidden-model\n"
            "provider: forbidden-provider\n"
        )
        home = tmp_path / "hermes-home"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))

        import cron.jobs as jobs_mod

        monkeypatch.setattr(jobs_mod, "load_jobs", list)
        monkeypatch.setattr(
            jobs_mod,
            "create_job",
            lambda **kwargs: (_ for _ in ()).throw(AssertionError("must not create")),
        )

        assert mod.main(["register_evolution_cron.py", str(src_dir)]) == 2

    def test_existing_pins_and_snapshots_are_cleared(self, tmp_path, monkeypatch):
        mod = _import_module()
        src_dir = tmp_path / "cron-src"
        src_dir.mkdir()
        (src_dir / "stage.yaml").write_text(
            "name: evolution-stage\n"
            'schedule: "0 9 * * *"\n'
            'prompt: "do the thing"\n'
        )
        home = tmp_path / "hermes-home"
        (home / "scripts").mkdir(parents=True)
        monkeypatch.setenv("HERMES_HOME", str(home))

        import cron.jobs as jobs_mod

        schedule = jobs_mod.parse_schedule("0 9 * * *")
        existing = {
            "id": "job-1",
            "name": "evolution-stage",
            "schedule": schedule,
            "schedule_display": schedule["display"],
            "prompt": "do the thing",
            "model": "old-model",
            "provider": "old-provider",
            "model_snapshot": "old-model",
            "provider_snapshot": "old-provider",
        }
        calls = []
        monkeypatch.setattr(jobs_mod, "load_jobs", lambda: [existing])
        monkeypatch.setattr(
            jobs_mod,
            "update_job",
            lambda job_id, updates: calls.append((job_id, updates)),
        )

        assert mod.main(["register_evolution_cron.py", str(src_dir)]) == 0
        assert calls == [
            (
                "job-1",
                {
                    "model": None,
                    "provider": None,
                    "model_snapshot": None,
                    "provider_snapshot": None,
                },
            ),
            ("job-1", {"model_snapshot": None, "provider_snapshot": None}),
        ]

    def test_real_storage_clears_recomputed_snapshots(self, tmp_path, monkeypatch):
        mod = _import_module()
        src_dir = tmp_path / "cron-src"
        src_dir.mkdir()
        (src_dir / "stage.yaml").write_text(
            "name: evolution-stage\n"
            'schedule: "0 9 * * *"\n'
            'prompt: "do the thing"\n'
        )
        home = tmp_path / "hermes-home"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setattr(mod, "_ensure_evolution_labels", lambda *a, **k: [])
        monkeypatch.setattr(mod, "_install_access_gate", lambda *a, **k: None)
        monkeypatch.setattr(mod, "_install_evolution_helpers", lambda *a, **k: [])

        import cron.jobs as jobs_mod

        monkeypatch.setattr(jobs_mod, "get_hermes_home", lambda: home)
        schedule = jobs_mod.parse_schedule("0 9 * * *")
        jobs_mod.save_jobs(
            [
                {
                    "id": "job-1",
                    "name": "evolution-stage",
                    "schedule": schedule,
                    "schedule_display": schedule["display"],
                    "prompt": "do the thing",
                    "model": "old-model",
                    "provider": "old-provider",
                    "model_snapshot": "old-model",
                    "provider_snapshot": "old-provider",
                    "enabled": True,
                    "state": "scheduled",
                }
            ]
        )

        assert mod.main(["register_evolution_cron.py", str(src_dir)]) == 0
        stored = jobs_mod.load_jobs()[0]
        assert stored["model"] is None
        assert stored["provider"] is None
        assert stored["model_snapshot"] is None
        assert stored["provider_snapshot"] is None


class TestFindVenvPython:
    """The registrar self-locates the install venv interpreter so it runs with
    the full Hermes deps regardless of which python launched it (no human/agent
    has to pick `venv/bin/python` by hand)."""

    def test_finds_venv_python(self, tmp_path):
        mod = _import_module()
        venv_py = tmp_path / "venv" / "bin" / "python"
        venv_py.parent.mkdir(parents=True)
        venv_py.write_text("#!/bin/sh\n")
        venv_py.chmod(0o755)

        assert mod._find_venv_python(tmp_path) == str(venv_py)

    def test_returns_none_when_absent(self, tmp_path):
        mod = _import_module()
        assert mod._find_venv_python(tmp_path) is None

    def test_non_executable_is_ignored(self, tmp_path):
        mod = _import_module()
        venv_py = tmp_path / "venv" / "bin" / "python"
        venv_py.parent.mkdir(parents=True)
        venv_py.write_text("#!/bin/sh\n")
        venv_py.chmod(0o644)  # not executable
        assert mod._find_venv_python(tmp_path) is None


class TestInstallEvolutionHelpers:
    """The whole evolution_*.py family is mirrored into HERMES_HOME/scripts so a
    no_agent script's sibling import (funnel -> metrics/realized_impact) resolves
    from the one dir the scheduler executes from. Without this the import is
    silently swallowed and the dependent sidecar freezes — the deploy gap that
    let the funnel run against a stale copy on the server."""

    def _fake_repo(self, tmp_path):
        repo = tmp_path / "repo"
        scripts = repo / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "evolution_funnel.py").write_text("# funnel\n")
        (scripts / "evolution_metrics.py").write_text("# metrics\n")
        (scripts / "evolution_realized_impact.py").write_text("# realized\n")
        # A sibling that must NOT be picked up by the evolution_*.py glob.
        (scripts / "register_evolution_cron.py").write_text("# registrar\n")
        (scripts / "helper.py").write_text("# unrelated\n")
        return repo

    def test_installs_whole_family(self, tmp_path, monkeypatch):
        mod = _import_module()
        repo = self._fake_repo(tmp_path)
        home = tmp_path / "hermes-home"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))

        installed = mod._install_evolution_helpers(repo)

        assert sorted(installed) == [
            "evolution_funnel.py",
            "evolution_metrics.py",
            "evolution_realized_impact.py",
        ]
        scripts = home / "scripts"
        assert (scripts / "evolution_funnel.py").is_file()
        assert (scripts / "evolution_metrics.py").is_file()
        assert (scripts / "evolution_realized_impact.py").is_file()
        # The registrar itself and unrelated scripts are NOT installed by glob.
        assert not (scripts / "register_evolution_cron.py").exists()
        assert not (scripts / "helper.py").exists()

    def test_refreshes_stale_copies(self, tmp_path, monkeypatch):
        mod = _import_module()
        repo = self._fake_repo(tmp_path)
        (repo / "scripts" / "evolution_funnel.py").write_text("# NEW funnel\n")
        home = tmp_path / "hermes-home"
        (home / "scripts").mkdir(parents=True)
        stale = home / "scripts" / "evolution_funnel.py"
        stale.write_text("# stale\n")
        monkeypatch.setenv("HERMES_HOME", str(home))

        mod._install_evolution_helpers(repo)

        assert stale.read_text() == "# NEW funnel\n"

    def test_no_family_returns_empty(self, tmp_path, monkeypatch):
        mod = _import_module()
        repo = tmp_path / "repo"
        (repo / "scripts").mkdir(parents=True)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))

        assert mod._install_evolution_helpers(repo) == []

    def test_installs_only_imported_siblings_despite_unparseable_family_file(
        self, tmp_path, monkeypatch
    ):
        """Missing/non-local imports and one broken file cannot stop the install."""
        mod = _import_module()
        repo = self._fake_repo(tmp_path)
        scripts = repo / "scripts"
        (scripts / "delegation_escalation_report.py").write_text("# report\n", encoding="utf-8")
        (scripts / "evolution_funnel.py").write_text(
            "import json\n"
            "import math\n"
            "from pathlib import Path\n"
            "from evolution.lib.telemetry_guard import validate\n"
            "from absent_sibling import missing\n"
            "try:\n"
            "    from scripts.delegation_escalation_report import summarize\n"
            "except ModuleNotFoundError:\n"
            "    from delegation_escalation_report import summarize\n",
            encoding="utf-8",
        )
        (scripts / "evolution_broken.py").write_text("def (:\n", encoding="utf-8")
        home = tmp_path / "hermes-home"
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(home))

        installed = mod._install_evolution_helpers(repo)

        assert set(installed) == {
            "evolution_funnel.py", "evolution_metrics.py",
            "evolution_realized_impact.py", "evolution_broken.py",
            "delegation_escalation_report.py",
        }
        assert {path.name for path in (home / "scripts").iterdir()} == set(installed)
        for unrelated in ("json.py", "absent_sibling.py", "helper.py",
                          "register_evolution_cron.py"):
            assert not (home / "scripts" / unrelated).exists()

    def test_installed_funnel_runs_with_and_without_optional_sidecar(
        self, tmp_path, monkeypatch
    ):
        """Real installed scripts must run without checkout or editable imports."""
        mod = _import_module()
        home = tmp_path / "hermes-home"
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("HERMES_HOME", str(home))
        mod._install_evolution_helpers(SCRIPT.parents[1])
        installed = home / "scripts"
        evolution = home / "evolution"
        trajectories = evolution / "trajectories"
        trajectories.mkdir(parents=True)
        date = "2026-10-15"
        (trajectories / "example.json").write_text(json.dumps({
            "date": date,
            "entries": [{"tool": "delegation_escalation_report", "args_summary": {
                "schema": "delegation-escalation-v1", "contacts": 3,
                "consultations": 1, "added_cost_usd": 0.25,
            }}],
        }), encoding="utf-8")
        (evolution / "integration").mkdir()
        (evolution / "integration" / f"{date}.json").write_text(
            json.dumps({"merged": [], "skipped": []}), encoding="utf-8"
        )
        # No credentials, gh/git executables, PYTHONPATH, or site-packages.
        # -S excludes editable checkout .pth files; the scripts use stdlib only.
        env = {
            "HOME": str(tmp_path), "USERPROFILE": str(tmp_path),
            "HERMES_HOME": str(home), "EVOLUTION_PROFILE_DIR": str(evolution),
            "PATH": "", "TZ": "UTC", "LANG": "C.UTF-8",
        }
        if "SYSTEMROOT" in os.environ:
            env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]

        def run(*args, cwd=tmp_path):
            result = subprocess.run(
                [sys.executable, "-E", "-S", "-B", *map(str, args)],
                cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8", timeout=30, check=False,
            )
            assert result.returncode == 0, result.stdout + result.stderr
            return result

        run(installed / "evolution_funnel.py", date)
        record = json.loads((evolution / "metrics.jsonl").read_text(encoding="utf-8").strip())
        sidecar = record["delegation_escalation"]
        assert sidecar["runs"] == 1
        assert sidecar["contacts"] == 3
        assert sidecar["consultations"] == 1
        assert sidecar["added_cost_usd"] == 0.25
        assert record["date"] == date
        assert record["merged"] == 0
        assert (evolution / "funnel-summary.txt").is_file()

        # Resolve both real non-prefixed dependencies from the installed directory.
        # The evaluator must actually load its dataset, not just swallow ImportError.
        imports = run("-c", (
            "import json, pathlib, delegation_escalation_report as report; "
            "import evolution_evaluator as evaluator; import eval_tasks; "
            "assert evaluator._EVAL_TASK_SET_OK; "
            "assert eval_tasks.load_task_set(); "
            "print(json.dumps([str(pathlib.Path(m.__file__).resolve()) "
            "for m in (report, evaluator, eval_tasks)]))"
        ), cwd=installed)
        assert all(Path(path).parent == installed
                   for path in json.loads(imports.stdout))

        # A partial/old install must still record the cycle and warn, using the
        # real import failure in a fresh interpreter rather than a patched global.
        (installed / "delegation_escalation_report.py").unlink()
        degraded = run(installed / "evolution_funnel.py", date)
        record = json.loads((evolution / "metrics.jsonl").read_text(encoding="utf-8").strip())
        assert record["delegation_escalation"] is None
        assert record["date"] == date
        assert record["merged"] == 0
        assert "delegation_escalation_report is not importable" in degraded.stderr


class TestEnsureEvolutionLabels:
    """``_ensure_evolution_labels`` idempotently creates the GitHub labels used
    by every evolution skill.  It must succeed when labels already exist and
    surface (but not die on) genuine gh failures."""

    def test_dry_run_lists_all_labels(self, tmp_path):
        mod = _import_module()
        ensured = mod._ensure_evolution_labels(tmp_path, dry_run=True)
        assert set(ensured) == {name for name, _, _ in mod._EVOLUTION_LABELS}

    def test_already_existing_label_is_confirmed(self, tmp_path, monkeypatch):
        mod = _import_module()
        calls = []
        monkeypatch.setattr(mod.shutil, "which", lambda name: "/usr/bin/gh")

        def fake_run(cmd, **kwargs):
            class _Result:
                returncode = 1
                stderr = f"HTTP 422: {cmd[3]} already exists"
                stdout = ""

            calls.append(cmd)
            return _Result()

        monkeypatch.setattr("subprocess.run", fake_run)
        ensured = mod._ensure_evolution_labels(tmp_path, dry_run=False)
        assert set(ensured) == {name for name, _, _ in mod._EVOLUTION_LABELS}
        assert len(calls) == len(mod._EVOLUTION_LABELS)
        # cmd layout: gh label create <name> --repo <repo> --color <c> --description <d>
        assert all(c[0] == "gh" and c[1] == "label" and c[2] == "create" for c in calls)
        assert {c[3] for c in calls} == {name for name, _, _ in mod._EVOLUTION_LABELS}
        assert all(
            "--repo" in c and "--color" in c and "--description" in c for c in calls
        )

    def test_real_failure_is_warning_not_fatal(self, tmp_path, monkeypatch, capsys):
        mod = _import_module()
        bad_label = None
        monkeypatch.setattr(mod.shutil, "which", lambda name: "/usr/bin/gh")

        def fake_run(cmd, **kwargs):
            class _Result:
                returncode = 1
                stderr = "HTTP 403: Forbidden"
                stdout = ""

            nonlocal bad_label
            bad_label = cmd[3]
            return _Result()

        monkeypatch.setattr("subprocess.run", fake_run)
        ensured = mod._ensure_evolution_labels(tmp_path, dry_run=False)
        assert ensured == []
        captured = capsys.readouterr()
        assert "warning: could not create label" in captured.err
        assert bad_label in captured.err

    def test_missing_gh_warns_once_without_spawning(
        self, tmp_path, monkeypatch, capsys
    ):
        mod = _import_module()
        monkeypatch.setattr(mod.shutil, "which", lambda name: None)
        calls = []
        monkeypatch.setattr(
            "subprocess.run", lambda *args, **kwargs: calls.append((args, kwargs))
        )

        ensured = mod._ensure_evolution_labels(tmp_path, dry_run=False)

        assert ensured == []
        assert calls == []
        captured = capsys.readouterr()
        assert captured.err.count("gh CLI is not installed") == 1


def _import_lint_module():
    import importlib.util

    path = SCRIPT.parent / "evolution_skill_lint.py"
    spec = importlib.util.spec_from_file_location("evolution_skill_lint_t", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestValidateSkillToolsets:
    """Pure pre-flight helper: block only the no_terminal class (#702)."""

    def test_blocks_when_terminal_missing(self):
        mod = _import_module()
        lint = _import_lint_module()
        texts = {"evolution-fake": "Run `python3 scripts/fake_tool.py` next."}
        scripts = {"scripts/fake_tool.py"}
        err = mod._validate_skill_toolsets(
            "job-x", ["evolution/fake"], ["web", "file"], lint, texts, scripts
        )
        assert err is not None
        assert "terminal" in err
        assert "evolution-fake" in err

    def test_passes_with_terminal_granted(self):
        mod = _import_module()
        lint = _import_lint_module()
        texts = {"evolution-fake": "Run `python3 scripts/fake_tool.py` next."}
        scripts = {"scripts/fake_tool.py"}
        err = mod._validate_skill_toolsets(
            "job-x", ["evolution/fake"], ["web", "terminal"], lint, texts, scripts
        )
        assert err is None

    def test_missing_script_warns_but_does_not_block(self, capsys):
        mod = _import_module()
        lint = _import_lint_module()
        texts = {"evolution-fake": "Run `python3 scripts/gone.py` next."}
        err = mod._validate_skill_toolsets(
            "job-x", ["evolution/fake"], ["web"], lint, texts, set()
        )
        assert err is None
        assert "does not exist" in capsys.readouterr().err

    def test_skips_without_lint_module_or_skills(self):
        mod = _import_module()
        lint = _import_lint_module()
        assert (
            mod._validate_skill_toolsets("j", ["s"], ["web"], None, {}, set()) is None
        )
        assert mod._validate_skill_toolsets("j", None, ["web"], lint, {}, set()) is None


class TestSkillToolsetPreflightRegistration:
    """Registration refuses agent jobs whose skills need a toolset the job
    does not grant (#702) — same wiring check as CI's evolution_skill_lint,
    enforced BEFORE the broken job can ever be scheduled."""

    def _write_yaml(self, src_dir, toolsets_lines):
        # evolution-introspection's SKILL.md instructs running scripts/*.py,
        # so a job granting no terminal can never execute them.
        (src_dir / "introspection.yaml").write_text(
            "name: evolution-preflight-test\n"
            'schedule: "0 20 * * *"\n'
            'prompt: "introspect"\n'
            "skills:\n  - evolution/introspection\n" + toolsets_lines
        )

    def _run(self, tmp_path, monkeypatch, toolsets_lines):
        mod = _import_module()
        src_dir = tmp_path / "cron-src"
        src_dir.mkdir()
        self._write_yaml(src_dir, toolsets_lines)
        home = tmp_path / "hermes-home"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setattr(mod, "_ensure_evolution_labels", lambda *a, **k: [])

        created = []

        def fake_create_job(**kwargs):
            created.append(kwargs)
            return {"id": "pf1", "name": kwargs["name"]}

        import cron.jobs as jobs_mod

        monkeypatch.setattr(jobs_mod, "create_job", fake_create_job)
        monkeypatch.setattr(jobs_mod, "load_jobs", list)

        rc = mod.main(["register_evolution_cron.py", str(src_dir)])
        return rc, created

    def test_missing_terminal_is_rejected(self, tmp_path, monkeypatch, capsys):
        rc, created = self._run(
            tmp_path, monkeypatch, "toolsets:\n  - web\n  - file\n"
        )
        assert rc == 2
        assert created == []
        out = capsys.readouterr().out
        assert "toolset pre-flight" in out
        assert "terminal" in out

    def test_with_terminal_registers_normally(self, tmp_path, monkeypatch):
        rc, created = self._run(
            tmp_path, monkeypatch, "toolsets:\n  - web\n  - file\n  - terminal\n"
        )
        assert rc == 0
        assert len(created) == 1
        assert created[0]["name"] == "evolution-preflight-test"

    def test_omitted_toolsets_field_is_exempt(self, tmp_path, monkeypatch):
        # No `toolsets:` in the YAML → enabled_toolsets stays None and the
        # scheduler falls back to the platform default toolset (which has
        # terminal) — the pre-flight must not block that (consult review).
        rc, created = self._run(tmp_path, monkeypatch, "")
        assert rc == 0
        assert len(created) == 1


class TestCadenceAndWriteGate:
    def test_pick_schedule_chat_safe_vs_dense(self):
        mod = _import_module()
        spec = {
            "schedule": "30 9 * * 1",
            "schedule_dense": "30 9 * * *",
        }
        assert mod._pick_schedule(spec, "chat-safe", "evolution-hydra", 7) == "30 9 * * 1"
        assert mod._pick_schedule(spec, "dense", "evolution-hydra", 7) == "30 9 * * *"

    def test_pick_schedule_research_interval_override(self):
        mod = _import_module()
        spec = {"schedule": "30 9 * * 1"}
        assert mod._pick_schedule(spec, "chat-safe", "evolution-research", 5) == "every 5d"
        assert mod._pick_schedule(spec, "dense", "evolution-research", 5) == "30 9 * * 1"

    def test_denied_skips_new_class_a_job(self, tmp_path, monkeypatch):
        mod = _import_module()
        mod._classify_write_access = lambda: "denied"
        src_dir = tmp_path / "cron-src"
        src_dir.mkdir()
        (src_dir / "research.yaml").write_text(
            "name: evolution-research\n"
            'schedule: "30 9 * * 1"\n'
            'prompt: "research"\n'
            "skills:\n  - evolution/research\n"
            "toolsets:\n  - web\n  - file\n"
        )
        home = tmp_path / "hermes-home"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        created = []

        import cron.jobs as jobs_mod

        monkeypatch.setattr(
            jobs_mod,
            "create_job",
            lambda **kw: created.append(kw) or {"id": "x", "name": kw["name"]},
        )
        monkeypatch.setattr(jobs_mod, "load_jobs", list)
        rc = mod.main(["register_evolution_cron.py", str(src_dir)])
        assert rc == 0
        assert created == []

    def test_denied_pauses_existing_class_a(self, tmp_path, monkeypatch):
        mod = _import_module()
        mod._classify_write_access = lambda: "denied"
        src_dir = tmp_path / "cron-src"
        src_dir.mkdir()
        (src_dir / "research.yaml").write_text(
            "name: evolution-research\n"
            'schedule: "30 9 * * 1"\n'
            'prompt: "research"\n'
            "skills:\n  - evolution/research\n"
            "toolsets:\n  - web\n  - file\n"
        )
        home = tmp_path / "hermes-home"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        paused = []

        import cron.jobs as jobs_mod

        existing = {
            "id": "res-1",
            "name": "evolution-research",
            "schedule": jobs_mod.parse_schedule("30 9 * * 1"),
            "prompt": "research",
        }
        monkeypatch.setattr(jobs_mod, "load_jobs", lambda: [existing])
        monkeypatch.setattr(
            jobs_mod,
            "pause_job",
            lambda job_id, reason=None: paused.append((job_id, reason)),
        )
        monkeypatch.setattr(
            jobs_mod,
            "create_job",
            lambda **kw: (_ for _ in ()).throw(AssertionError("must not create")),
        )
        rc = mod.main(["register_evolution_cron.py", str(src_dir)])
        assert rc == 0
        assert paused == [("res-1", mod.PAUSE_REASON_NO_WRITE)]

    def test_inconclusive_does_not_pause_existing(self, tmp_path, monkeypatch):
        mod = _import_module()
        mod._classify_write_access = lambda: "inconclusive"
        src_dir = tmp_path / "cron-src"
        src_dir.mkdir()
        (src_dir / "research.yaml").write_text(
            "name: evolution-research\n"
            'schedule: "30 9 * * 1"\n'
            'prompt: "research"\n'
            "skills:\n  - evolution/research\n"
            "toolsets:\n  - web\n  - file\n"
        )
        home = tmp_path / "hermes-home"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        paused = []
        updates = []

        import cron.jobs as jobs_mod

        sched = jobs_mod.parse_schedule("30 9 * * 1")
        existing = {
            "id": "res-1",
            "name": "evolution-research",
            "schedule": sched,
            "schedule_display": sched.get("display"),
            "prompt": "research",
            "skills": mod._normalize_skills(["evolution/research"]),
            "enabled_toolsets": mod._normalize_toolsets(["web", "file"]),
        }
        monkeypatch.setattr(jobs_mod, "load_jobs", lambda: [existing])
        monkeypatch.setattr(
            jobs_mod,
            "pause_job",
            lambda job_id, reason=None: paused.append((job_id, reason)),
        )
        monkeypatch.setattr(
            jobs_mod,
            "update_job",
            lambda job_id, updates_dict: updates.append((job_id, updates_dict)),
        )
        rc = mod.main(["register_evolution_cron.py", str(src_dir)])
        assert rc == 0
        assert paused == []
        # May reconcile snapshots; must not disable the job.
        assert all("enabled" not in (u or {}) for _, u in updates)

    def test_watchdog_still_registers_when_write_denied(self, tmp_path, monkeypatch):
        mod = _import_module()
        mod._classify_write_access = lambda: "denied"
        src_dir = tmp_path / "cron-src"
        src_dir.mkdir()
        (src_dir / "watchdog.yaml").write_text(
            "name: evolution-watchdog\n"
            'schedule: "47 7 * * *"\n'
            "enabled: true\n"
            "no_agent: true\n"
            "script: evolution_watchdog.py\n"
            "deliver: all\n"
            'prompt: "health check"\n'
        )
        home = tmp_path / "hermes-home"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        created = []

        import cron.jobs as jobs_mod

        monkeypatch.setattr(
            jobs_mod,
            "create_job",
            lambda **kw: created.append(kw) or {"id": "wd", "name": kw["name"]},
        )
        monkeypatch.setattr(jobs_mod, "load_jobs", list)
        rc = mod.main(["register_evolution_cron.py", str(src_dir)])
        assert rc == 0
        assert len(created) == 1
        assert created[0]["name"] == "evolution-watchdog"
