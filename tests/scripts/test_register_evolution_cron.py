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
        assert calls["updates"] == {"script": "evolution_analysis_gate.sh"}
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
        # Only the schedule reconciles; the registered skills/toolsets must be
        # preserved (not clobbered to []) when the YAML omits them.
        assert calls["updates"] == {"schedule": "0 8 * * 1,3,5"}


class TestModelProviderAllocation:
    """Per-stage model allocation (#905): a stage YAML may set optional
    model:/provider: keys to pin that stage to a specific model (e.g. a
    cheaper/mid-tier model for research/analysis, leaving implementation
    unpinned on the deployment's frontier default). Both fields are
    independent, optional, and pass straight through to
    cron.jobs.create_job/update_job unchanged — this class only tests that
    the registrar reads and wires them correctly, mirroring the
    skills/toolsets reconcile semantics above (None means leave as-is)."""

    def _write_yaml(self, src_dir, extra="", schedule="0 9 * * *"):
        (src_dir / "cheap-stage.yaml").write_text(
            "name: evolution-cheap-stage\n"
            f'schedule: "{schedule}"\n'
            "enabled: true\n"
            'prompt: "do the thing"\n' + extra
        )

    def test_create_job_passes_model_and_provider_when_set(self, tmp_path, monkeypatch):
        mod = _import_module()
        src_dir = tmp_path / "cron-src"
        src_dir.mkdir()
        self._write_yaml(src_dir, "model: glm-5-flash\nprovider: zai\n")
        home = tmp_path / "hermes-home"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))

        captured = {}

        def fake_create_job(**kwargs):
            captured.update(kwargs)
            return {"id": "job-1", "name": kwargs["name"]}

        import cron.jobs as jobs_mod

        monkeypatch.setattr(jobs_mod, "create_job", fake_create_job)
        monkeypatch.setattr(jobs_mod, "load_jobs", lambda: [])

        rc = mod.main(["register_evolution_cron.py", str(src_dir)])

        assert rc == 0
        assert captured["model"] == "glm-5-flash"
        assert captured["provider"] == "zai"

    def test_create_job_omits_model_and_provider_leaves_them_none(
        self, tmp_path, monkeypatch
    ):
        """A YAML with no model:/provider: keys must produce the exact same
        create_job call as before #905 (both None) — the unpinned, back-compat
        default that follows the deployment's global config."""
        mod = _import_module()
        src_dir = tmp_path / "cron-src"
        src_dir.mkdir()
        self._write_yaml(src_dir)  # no model:/provider: keys
        home = tmp_path / "hermes-home"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))

        captured = {}

        def fake_create_job(**kwargs):
            captured.update(kwargs)
            return {"id": "job-1", "name": kwargs["name"]}

        import cron.jobs as jobs_mod

        monkeypatch.setattr(jobs_mod, "create_job", fake_create_job)
        monkeypatch.setattr(jobs_mod, "load_jobs", lambda: [])

        rc = mod.main(["register_evolution_cron.py", str(src_dir)])

        assert rc == 0
        assert captured["model"] is None
        assert captured["provider"] is None

    def _existing(self, jobs_mod, schedule, model=None, provider=None):
        sched = jobs_mod.parse_schedule(schedule)
        job = {
            "id": "job-9",
            "name": "evolution-cheap-stage",
            "schedule": sched,
            "schedule_display": sched.get("display"),
            "prompt": "do the thing",
        }
        if model is not None:
            job["model"] = model
        if provider is not None:
            job["provider"] = provider
        return job

    def _wire(self, jobs_mod, monkeypatch, tmp_path, existing):
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

    def test_reconcile_updates_model_when_changed(self, tmp_path, monkeypatch):
        mod = _import_module()
        import cron.jobs as jobs_mod

        src_dir = tmp_path / "cron-src"
        src_dir.mkdir()
        self._write_yaml(src_dir, "model: glm-5-flash\n")
        existing = self._existing(jobs_mod, "0 9 * * *", model="old-model")
        calls = self._wire(jobs_mod, monkeypatch, tmp_path, existing)

        rc = mod.main(["register_evolution_cron.py", str(src_dir)])

        assert rc == 0
        assert calls["updates"] == {"model": "glm-5-flash"}

    def test_reconcile_updates_provider_when_changed(self, tmp_path, monkeypatch):
        mod = _import_module()
        import cron.jobs as jobs_mod

        src_dir = tmp_path / "cron-src"
        src_dir.mkdir()
        self._write_yaml(src_dir, "provider: zai\n")
        existing = self._existing(jobs_mod, "0 9 * * *", provider="old-provider")
        calls = self._wire(jobs_mod, monkeypatch, tmp_path, existing)

        rc = mod.main(["register_evolution_cron.py", str(src_dir)])

        assert rc == 0
        assert calls["updates"] == {"provider": "zai"}

    def test_reconcile_unchanged_model_and_provider_trigger_no_update(
        self, tmp_path, monkeypatch
    ):
        mod = _import_module()
        import cron.jobs as jobs_mod

        src_dir = tmp_path / "cron-src"
        src_dir.mkdir()
        self._write_yaml(src_dir, "model: glm-5-flash\nprovider: zai\n")
        existing = self._existing(
            jobs_mod, "0 9 * * *", model="glm-5-flash", provider="zai"
        )
        calls = self._wire(jobs_mod, monkeypatch, tmp_path, existing)

        rc = mod.main(["register_evolution_cron.py", str(src_dir)])

        assert rc == 0
        assert calls == {}  # nothing changed anywhere -> no update_job call

    def test_reconcile_omitted_model_and_provider_preserve_pinned_values(
        self, tmp_path, monkeypatch
    ):
        """A YAML that never mentions model:/provider: must NOT clear an
        already-pinned job back to unpinned — mirrors the skills/toolsets
        back-compat guarantee in TestReconcileExistingJob above."""
        mod = _import_module()
        import cron.jobs as jobs_mod

        src_dir = tmp_path / "cron-src"
        src_dir.mkdir()
        # Changed schedule (so update_job IS called), but no model:/provider:.
        self._write_yaml(src_dir)
        existing = self._existing(
            jobs_mod, "0 8 * * *", model="pinned-model", provider="pinned-provider"
        )
        calls = self._wire(jobs_mod, monkeypatch, tmp_path, existing)

        rc = mod.main(["register_evolution_cron.py", str(src_dir)])

        assert rc == 0
        assert calls["updates"] == {"schedule": "0 9 * * *"}
        assert "model" not in calls["updates"]
        assert "provider" not in calls["updates"]


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
        monkeypatch.setattr(jobs_mod, "load_jobs", lambda: [])

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
