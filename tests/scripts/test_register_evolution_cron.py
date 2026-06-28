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
            'name: evolution-bad\nschedule: "0 9 * * *"\nno_agent: true\nprompt: "x"\n'
        )
        home = tmp_path / "hermes-home"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))

        import cron.jobs as jobs_mod

        monkeypatch.setattr(jobs_mod, "load_jobs", lambda: [])

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
        # only the schedule changed → only the schedule is updated
        assert calls["updates"] == {"schedule": "0 8 * * 1,3,5"}

    def test_unchanged_job_is_left_alone(self, tmp_path, monkeypatch):
        mod = _import_module()
        import cron.jobs as jobs_mod

        src_dir = tmp_path / "cron-src"
        src_dir.mkdir()
        self._write_agent_yaml(src_dir, "0 8 * * 1,3,5")
        existing = self._existing(mod, jobs_mod, "0 8 * * 1,3,5")  # matches YAML
        calls = self._wire(mod, jobs_mod, monkeypatch, tmp_path, existing)

        rc = mod.main(["register_evolution_cron.py", str(src_dir)])

        assert rc == 0
        assert calls == {}  # no update_job call — nothing changed

    def _write_agent_yaml_no_skills(self, src_dir, schedule):
        # An agent job whose YAML omits skills:/toolsets: entirely — the
        # _normalize_skills(None) -> None case that used to crash reconcile.
        (src_dir / "upstream-sync.yaml").write_text(
            "name: evolution-upstream-sync\n"
            f'schedule: "{schedule}"\n'
            "enabled: true\n"
            'prompt: "sync upstream"\n'
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
        # Only the schedule reconciles; the registered skills/toolsets must be
        # preserved (not clobbered to []) when the YAML omits them.
        assert calls["updates"] == {"schedule": "0 8 * * 1,3,5"}



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
