"""Tests for per-job workdir support in cron jobs.

Covers:
  - jobs.create_job: param plumbing, validation, default-None preserved
  - jobs._normalize_workdir: absolute / relative / missing / file-not-dir
  - jobs.update_job: set, clear, re-validate
  - tools.cronjob_tools.cronjob: create + update JSON round-trip, schema
    includes workdir, _format_job exposes it when set
  - scheduler.tick(): partitions workdir jobs off the thread pool, restores
    TERMINAL_CWD in finally, honours the env override during run_job
"""

from __future__ import annotations

import json
import os

import pytest


@pytest.fixture()
def tmp_cron_dir(tmp_path, monkeypatch):
    """Isolate cron job storage into a temp dir so tests don't stomp on real jobs."""
    monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")
    return tmp_path


# ---------------------------------------------------------------------------
# jobs._normalize_workdir
# ---------------------------------------------------------------------------

class TestNormalizeWorkdir:
    def test_none_returns_none(self):
        from cron.jobs import _normalize_workdir
        assert _normalize_workdir(None) is None

    def test_empty_string_returns_none(self):
        from cron.jobs import _normalize_workdir
        assert _normalize_workdir("") is None
        assert _normalize_workdir("   ") is None

    def test_absolute_existing_dir_returns_resolved_str(self, tmp_path):
        from cron.jobs import _normalize_workdir
        result = _normalize_workdir(str(tmp_path))
        assert result == str(tmp_path.resolve())

    def test_tilde_expands(self, tmp_path, monkeypatch):
        from cron.jobs import _normalize_workdir
        monkeypatch.setenv("HOME", str(tmp_path))
        result = _normalize_workdir("~")
        assert result == str(tmp_path.resolve())

    def test_relative_path_rejected(self):
        from cron.jobs import _normalize_workdir
        with pytest.raises(ValueError, match="absolute path"):
            _normalize_workdir("some/relative/path")

    def test_missing_dir_rejected(self, tmp_path):
        from cron.jobs import _normalize_workdir
        missing = tmp_path / "does-not-exist"
        with pytest.raises(ValueError, match="does not exist"):
            _normalize_workdir(str(missing))

    def test_file_not_dir_rejected(self, tmp_path):
        from cron.jobs import _normalize_workdir
        f = tmp_path / "file.txt"
        f.write_text("hi")
        with pytest.raises(ValueError, match="not a directory"):
            _normalize_workdir(str(f))


# ---------------------------------------------------------------------------
# jobs.create_job and update_job
# ---------------------------------------------------------------------------

class TestCreateJobWorkdir:
    def test_workdir_stored_when_set(self, tmp_cron_dir):
        from cron.jobs import create_job, get_job
        job = create_job(
            prompt="hello",
            schedule="every 1h",
            workdir=str(tmp_cron_dir),
        )
        stored = get_job(job["id"])
        assert stored["workdir"] == str(tmp_cron_dir.resolve())

    def test_workdir_none_preserves_old_behaviour(self, tmp_cron_dir):
        from cron.jobs import create_job, get_job
        job = create_job(prompt="hello", schedule="every 1h")
        stored = get_job(job["id"])
        # Field is present on the dict but None — downstream code checks
        # truthiness to decide whether the feature is active.
        assert stored.get("workdir") is None

    def test_create_rejects_invalid_workdir(self, tmp_cron_dir):
        from cron.jobs import create_job
        with pytest.raises(ValueError):
            create_job(
                prompt="hello",
                schedule="every 1h",
                workdir="not/absolute",
            )


class TestUpdateJobWorkdir:
    def test_set_workdir_via_update(self, tmp_cron_dir):
        from cron.jobs import create_job, get_job, update_job
        job = create_job(prompt="x", schedule="every 1h")
        update_job(job["id"], {"workdir": str(tmp_cron_dir)})
        assert get_job(job["id"])["workdir"] == str(tmp_cron_dir.resolve())

    def test_clear_workdir_with_none(self, tmp_cron_dir):
        from cron.jobs import create_job, get_job, update_job
        job = create_job(
            prompt="x", schedule="every 1h", workdir=str(tmp_cron_dir)
        )
        update_job(job["id"], {"workdir": None})
        assert get_job(job["id"])["workdir"] is None

    def test_clear_workdir_with_empty_string(self, tmp_cron_dir):
        from cron.jobs import create_job, get_job, update_job
        job = create_job(
            prompt="x", schedule="every 1h", workdir=str(tmp_cron_dir)
        )
        update_job(job["id"], {"workdir": ""})
        assert get_job(job["id"])["workdir"] is None

    def test_update_rejects_invalid_workdir(self, tmp_cron_dir):
        from cron.jobs import create_job, update_job
        job = create_job(prompt="x", schedule="every 1h")
        with pytest.raises(ValueError):
            update_job(job["id"], {"workdir": "nope/relative"})


# ---------------------------------------------------------------------------
# tools.cronjob_tools: end-to-end JSON round-trip
# ---------------------------------------------------------------------------

class TestCronjobToolWorkdir:
    def test_create_with_workdir_json_roundtrip(self, tmp_cron_dir):
        from tools.cronjob_tools import cronjob

        result = json.loads(
            cronjob(
                action="create",
                prompt="hi",
                schedule="every 1h",
                workdir=str(tmp_cron_dir),
            )
        )
        assert result["success"] is True
        assert result["job"]["workdir"] == str(tmp_cron_dir.resolve())

    def test_create_without_workdir_hides_field_in_format(self, tmp_cron_dir):
        from tools.cronjob_tools import cronjob

        result = json.loads(
            cronjob(
                action="create",
                prompt="hi",
                schedule="every 1h",
            )
        )
        assert result["success"] is True
        # _format_job omits the field when unset — reduces noise in agent output.
        assert "workdir" not in result["job"]

    def test_update_clears_workdir_with_empty_string(self, tmp_cron_dir):
        from tools.cronjob_tools import cronjob

        created = json.loads(
            cronjob(
                action="create",
                prompt="hi",
                schedule="every 1h",
                workdir=str(tmp_cron_dir),
            )
        )
        job_id = created["job_id"]

        updated = json.loads(
            cronjob(action="update", job_id=job_id, workdir="")
        )
        assert updated["success"] is True
        assert "workdir" not in updated["job"]

    def test_schema_advertises_workdir(self):
        from tools.cronjob_tools import CRONJOB_SCHEMA
        assert "workdir" in CRONJOB_SCHEMA["parameters"]["properties"]
        desc = CRONJOB_SCHEMA["parameters"]["properties"]["workdir"]["description"]
        assert "absolute" in desc.lower()


# ---------------------------------------------------------------------------
# scheduler.tick(): workdir partition
# ---------------------------------------------------------------------------

class TestTickWorkdirPartition:
    """
    tick() must run workdir jobs sequentially (outside the ThreadPoolExecutor)
    because run_job mutates os.environ["TERMINAL_CWD"], which is process-global.
    We verify the partition without booting the real scheduler by patching the
    pieces tick() calls.
    """

    def test_workdir_jobs_run_sequentially(self, tmp_path, monkeypatch):
        import cron.scheduler as sched

        # Two workdir jobs (both sequential) + one parallel job.
        workdir_a = {"id": "a", "name": "A", "workdir": str(tmp_path)}
        workdir_b = {"id": "b", "name": "B", "workdir": str(tmp_path)}
        parallel_job = {"id": "c", "name": "C", "workdir": None}

        monkeypatch.setattr(sched, "get_due_jobs", lambda: [workdir_a, workdir_b, parallel_job])
        monkeypatch.setattr(sched, "advance_next_run", lambda *_a, **_kw: None)

        # Record call order / thread context.
        import threading
        calls: list[tuple[str, str]] = []
        order_lock = threading.Lock()

        def fake_run_job(job):
            # Return a minimal tuple matching run_job's signature.
            with order_lock:
                calls.append((job["id"], threading.current_thread().name))
            return True, "output", "response", None

        monkeypatch.setattr(sched, "run_job", fake_run_job)
        monkeypatch.setattr(sched, "save_job_output", lambda _jid, _o: None)
        monkeypatch.setattr(sched, "mark_job_run", lambda *_a, **_kw: None)
        monkeypatch.setattr(
            sched, "_deliver_result", lambda *_a, **_kw: None
        )

        n = sched.tick(verbose=False)
        assert n == 3

        ids = [c[0] for c in calls]
        # Sequential workdir jobs preserve submission order relative to each
        # other (single-thread pool).
        assert ids.index("a") < ids.index("b")

        # Workdir jobs run on the persistent single-thread cron-seq pool —
        # NOT the main thread — so a long workdir job never blocks the ticker.
        main_thread_name = threading.current_thread().name
        for jid in ("a", "b"):
            workdir_thread_name = next(t for j, t in calls if j == jid)
            assert workdir_thread_name != main_thread_name
            assert workdir_thread_name.startswith("cron-seq"), workdir_thread_name
        par_thread_name = next(t for j, t in calls if j == "c")
        assert par_thread_name.startswith("cron-parallel"), par_thread_name


# ---------------------------------------------------------------------------
# scheduler.run_job: TERMINAL_CWD + skip_context_files wiring
# ---------------------------------------------------------------------------

class TestRunJobTerminalCwd:
    """
    run_job sets TERMINAL_CWD + flips skip_context_files=False when workdir
    is set, and restores the prior TERMINAL_CWD in finally — even on error.
    We stub AIAgent so no real API call happens.
    """

    @staticmethod
    def _install_stubs(monkeypatch, observed: dict, clobber_cwd: str | None = None):
        """Patch enough of run_job's deps that it executes without real creds.

        ``clobber_cwd``: if set, ``FakeAgent.__init__`` overwrites
        ``TERMINAL_CWD`` with it AFTER recording the value it saw — simulating
        the real regression where the agent's construction (worker-thread
        context restore + init) re-applied a stale TERMINAL_CWD, so the env was
        built in the primary deploy checkout instead of the job workdir.
        """
        import os
        import sys
        import cron.scheduler as sched

        class FakeAgent:
            def __init__(self, **kwargs):
                observed["skip_context_files"] = kwargs.get("skip_context_files")
                observed["load_soul_identity"] = kwargs.get("load_soul_identity")
                observed["terminal_cwd_during_init"] = os.environ.get(
                    "TERMINAL_CWD", "_UNSET_"
                )
                if clobber_cwd is not None:
                    os.environ["TERMINAL_CWD"] = clobber_cwd

            def run_conversation(self, *_a, **_kw):
                observed["terminal_cwd_during_run"] = os.environ.get(
                    "TERMINAL_CWD", "_UNSET_"
                )
                return {"final_response": "done", "messages": []}

            def get_activity_summary(self):
                return {"seconds_since_activity": 0.0}

        fake_mod = type(sys)("run_agent")
        fake_mod.AIAgent = FakeAgent
        monkeypatch.setitem(sys.modules, "run_agent", fake_mod)

        # Bypass the real provider resolver — it reads ~/.hermes and credentials.
        from hermes_cli import runtime_provider as _rtp
        monkeypatch.setattr(
            _rtp,
            "resolve_runtime_provider",
            lambda **_kw: {
                "provider": "test",
                "api_key": "k",
                "base_url": "http://test.local",
                "api_mode": "chat_completions",
            },
        )

        # Stub scheduler helpers that would otherwise hit the filesystem / config.
        monkeypatch.setattr(sched, "_build_job_prompt", lambda job, prerun_script=None: "hi")
        monkeypatch.setattr(sched, "_resolve_origin", lambda job: None)
        monkeypatch.setattr(sched, "_resolve_delivery_target", lambda job: None)
        monkeypatch.setattr(sched, "_resolve_cron_enabled_toolsets", lambda job, cfg: None)
        # Unlimited inactivity so the poll loop returns immediately.
        monkeypatch.setenv("HERMES_CRON_TIMEOUT", "0")

        # run_job calls load_dotenv(~/.hermes/.env, override=True), which will
        # happily clobber TERMINAL_CWD out from under us if the real user .env
        # has TERMINAL_CWD set (common on dev boxes).  Stub it out.
        import dotenv
        monkeypatch.setattr(dotenv, "load_dotenv", lambda *_a, **_kw: True)

    def test_workdir_sets_and_restores_terminal_cwd(
        self, tmp_path, monkeypatch
    ):
        import os
        import cron.scheduler as sched

        # Make sure the test's TERMINAL_CWD starts at a known non-workdir value.
        # Use monkeypatch.setenv so it's restored on teardown regardless of
        # whatever other tests in this xdist worker have left behind.
        monkeypatch.setenv("TERMINAL_CWD", "/original/cwd")

        observed: dict = {}
        self._install_stubs(monkeypatch, observed)

        job = {
            "id": "abc",
            "name": "wd-job",
            "workdir": str(tmp_path),
            "schedule_display": "manual",
        }

        success, _output, response, error = sched.run_job(job)
        assert success is True, f"run_job failed: error={error!r} response={response!r}"

        # AIAgent was built with skip_context_files=False (feature ON).
        assert observed["skip_context_files"] is False
        assert observed["load_soul_identity"] is True
        # TERMINAL_CWD was pointing at the job workdir while the agent ran.
        assert observed["terminal_cwd_during_init"] == str(tmp_path.resolve())
        assert observed["terminal_cwd_during_run"] == str(tmp_path.resolve())

        # And it was restored to the original value in finally.
        assert os.environ["TERMINAL_CWD"] == "/original/cwd"

    def test_workdir_survives_agent_init_clobber(self, tmp_path, monkeypatch):
        """Regression: the agent runs in a worker thread under a copied context;
        that context restore + the agent's own construction could re-apply a
        stale TERMINAL_CWD AFTER run_job set the job workdir, so the agent's
        terminal/file env was built in the gateway's primary DEPLOY checkout
        instead of the workdir — reading wrong paths (files "missing") and, for a
        git-worktree workdir, running git/edits against the LIVE checkout and
        corrupting the running deployment. run_job must re-assert the workdir
        override inside the worker, immediately before run_conversation, so the
        clobber cannot win."""
        import os
        import cron.scheduler as sched

        monkeypatch.setenv("TERMINAL_CWD", "/original/cwd")
        observed: dict = {}
        # Agent construction clobbers TERMINAL_CWD to a wrong absolute path,
        # mimicking the real deploy-checkout clobber.
        self._install_stubs(
            monkeypatch, observed, clobber_cwd="/wrong/deploy/checkout"
        )

        job = {
            "id": "abc",
            "name": "wd-job",
            "workdir": str(tmp_path),
            "schedule_display": "manual",
        }
        success, _output, response, error = sched.run_job(job)
        assert success is True, f"run_job failed: error={error!r} response={response!r}"

        # Construction recorded the workdir (set by run_job) BEFORE it clobbered...
        assert observed["terminal_cwd_during_init"] == str(tmp_path.resolve())
        # ...but by the time the agent RAN, the workdir override was re-asserted.
        # Without the fix this is "/wrong/deploy/checkout".
        assert observed["terminal_cwd_during_run"] == str(tmp_path.resolve())
        # And the prior value is still restored in finally.
        assert os.environ["TERMINAL_CWD"] == "/original/cwd"

    def test_no_workdir_leaves_terminal_cwd_untouched(self, monkeypatch):
        """When workdir is absent, run_job must not touch TERMINAL_CWD at all —
        whatever value was present before the call should be present after.

        We don't assert on the *content* of TERMINAL_CWD (other tests in the
        same xdist worker may leave it set to something like '.'); we just
        check it's unchanged by run_job.
        """
        import os
        import cron.scheduler as sched

        # Pin TERMINAL_CWD to a sentinel via monkeypatch so we control both
        # the before-value and the after-value regardless of cross-test state.
        monkeypatch.setenv("TERMINAL_CWD", "/cron-test-sentinel")
        before = os.environ["TERMINAL_CWD"]

        observed: dict = {}
        self._install_stubs(monkeypatch, observed)

        job = {
            "id": "xyz",
            "name": "no-wd-job",
            "workdir": None,
            "schedule_display": "manual",
        }

        success, *_ = sched.run_job(job)
        assert success is True

        # Feature is OFF — skip_context_files stays True.
        assert observed["skip_context_files"] is True
        # Cron still forces SOUL.md identity even when cwd context files stay off.
        assert observed["load_soul_identity"] is True
        # TERMINAL_CWD saw the same value during init as it had before.
        assert observed["terminal_cwd_during_init"] == before
        # And after run_job completes, it's still the sentinel (nothing
        # overwrote or cleared it).
        assert os.environ["TERMINAL_CWD"] == before


# ---------------------------------------------------------------------------
# scheduler.run_job: workdir redirects BOTH file-edit tools AND terminal/git
# ---------------------------------------------------------------------------

class TestRunJobWorkdirFileTools:
    """Regression for the cron-workdir split-brain.

    Setting ``TERMINAL_CWD`` alone redirected only the terminal/git tools. The
    file-edit tools resolve relative paths through
    ``tools/file_tools._resolve_base_dir``, whose top priority (#1) is the
    task's *live* terminal-env cwd. In a long-lived gateway/daemon the shared
    ``"default"`` env still points at the runtime checkout, so it shadowed
    ``TERMINAL_CWD`` (#3): git honoured the workdir while ``write_file`` landed
    in the runtime mirror, dirtying it. run_job must seed the live env cwd to
    the workdir so BOTH layers resolve there — and leave workdir-less jobs and
    the live env's prior cwd untouched.
    """

    @staticmethod
    def _seed_stale_default_env(runtime_dir):
        """Install a fake long-lived ``"default"`` terminal env whose live cwd
        points at *runtime_dir*. Returns a callable that restores prior state.
        """
        import tools.file_tools as ft
        import tools.terminal_tool as tt

        class _FakeEnv:
            def __init__(self, cwd):
                self.cwd = cwd

        _sentinel = object()
        prior_env = tt._active_environments.get("default", _sentinel)
        prior_override = tt._task_env_overrides.get("default", _sentinel)
        prior_fileops = ft._file_ops_cache.get("default", _sentinel)

        tt._active_environments["default"] = _FakeEnv(str(runtime_dir))
        tt._task_env_overrides.pop("default", None)
        ft._file_ops_cache.pop("default", None)

        def _restore():
            for store, key, val in (
                (tt._active_environments, "default", prior_env),
                (tt._task_env_overrides, "default", prior_override),
                (ft._file_ops_cache, "default", prior_fileops),
            ):
                if val is _sentinel:
                    store.pop(key, None)
                else:
                    store[key] = val

        return _restore

    @staticmethod
    def _install_capturing_agent(monkeypatch, observed):
        """Install the shared run_job stubs, then swap in a FakeAgent that, at
        run-conversation time, records where the file-edit tools and the live
        terminal env would resolve a relative path to.
        """
        import sys

        import tools.file_tools as ft
        import tools.terminal_tool as tt

        TestRunJobTerminalCwd._install_stubs(monkeypatch, observed)

        class CapturingAgent:
            def __init__(self, **kwargs):
                observed["skip_context_files"] = kwargs.get("skip_context_files")

            def run_conversation(self, *_a, **_kw):
                # File-edit tools (write_file/patch/...) resolve relative paths
                # against this base dir — the directory a `write_file` lands in.
                observed["base_dir"] = str(ft._resolve_base_dir("default"))
                # Terminal/git resolve commands against the live env cwd.
                env = tt._active_environments.get("default")
                observed["live_env_cwd"] = getattr(env, "cwd", None)
                observed["override_during_run"] = tt._task_env_overrides.get(
                    "default"
                )
                observed["terminal_cwd_env"] = os.environ.get("TERMINAL_CWD")
                return {"final_response": "done", "messages": []}

            def get_activity_summary(self):
                return {"seconds_since_activity": 0.0}

        fake_mod = type(sys)("run_agent")
        fake_mod.AIAgent = CapturingAgent
        monkeypatch.setitem(sys.modules, "run_agent", fake_mod)

    def test_workdir_redirects_file_tools_and_terminal(self, tmp_path, monkeypatch):
        from pathlib import Path

        import cron.scheduler as sched

        workdir = tmp_path / "worktree"
        workdir.mkdir()
        runtime = tmp_path / "runtime"
        runtime.mkdir()

        # A workdir job must never resolve file edits to the runtime mirror.
        monkeypatch.setenv("HERMES_MODEL", "test-model")
        monkeypatch.setenv("TERMINAL_CWD", str(runtime))

        restore = self._seed_stale_default_env(runtime)
        try:
            observed: dict = {}
            self._install_capturing_agent(monkeypatch, observed)

            job = {
                "id": "wd-ft",
                "name": "wd-ft-job",
                "workdir": str(workdir),
                "model": "test-model",
                "schedule_display": "manual",
            }

            success, _o, response, error = sched.run_job(job)
            assert success is True, f"run_job failed: error={error!r} response={response!r}"

            # PRE-FIX this is the runtime checkout (the stale env shadows
            # TERMINAL_CWD) — the split-brain. POST-FIX it is the workdir.
            assert observed["base_dir"] == str(workdir.resolve())
            # Terminal/git resolve to the workdir too.
            assert Path(observed["live_env_cwd"]).resolve() == workdir.resolve()
            assert observed["terminal_cwd_env"] == str(workdir)

            # The shared "default" env's live cwd is restored to its pre-job
            # value (the runtime checkout) — no leak into later jobs.
            import tools.terminal_tool as tt
            assert Path(tt._active_environments["default"].cwd).resolve() == runtime.resolve()
            # And the transient cwd override is gone.
            assert "default" not in tt._task_env_overrides
        finally:
            restore()

    def test_no_workdir_leaves_file_tool_resolution_untouched(self, tmp_path, monkeypatch):
        """A workdir-less job must NOT redirect the file tools: its relative
        edits keep resolving against the live (stale) env cwd exactly as before,
        and no transient cwd override is registered.
        """
        from pathlib import Path

        import cron.scheduler as sched
        import tools.terminal_tool as tt

        runtime = tmp_path / "runtime"
        runtime.mkdir()

        monkeypatch.setenv("HERMES_MODEL", "test-model")
        monkeypatch.setenv("TERMINAL_CWD", str(runtime))

        restore = self._seed_stale_default_env(runtime)
        try:
            observed: dict = {}
            self._install_capturing_agent(monkeypatch, observed)

            job = {
                "id": "no-wd-ft",
                "name": "no-wd-ft-job",
                "workdir": None,
                "model": "test-model",
                "schedule_display": "manual",
            }

            success, *_ = sched.run_job(job)
            assert success is True

            # Behaviour unchanged: resolution follows the live env (runtime).
            assert observed["base_dir"] == str(runtime.resolve())
            assert Path(observed["live_env_cwd"]).resolve() == runtime.resolve()
            # No cwd override was registered for a workdir-less job.
            assert observed["override_during_run"] is None
            assert "default" not in tt._task_env_overrides
        finally:
            restore()

    def test_sequential_workdir_then_no_workdir_no_leak(self, tmp_path, monkeypatch):
        """A workdir job must not leak its cwd into a later workdir-less job that
        reuses the same shared ``"default"`` env: after the workdir job restores
        the env, the next job resolves back to the runtime checkout.
        """
        from pathlib import Path

        import cron.scheduler as sched
        import tools.terminal_tool as tt

        workdir = tmp_path / "worktree"
        workdir.mkdir()
        runtime = tmp_path / "runtime"
        runtime.mkdir()

        monkeypatch.setenv("HERMES_MODEL", "test-model")
        monkeypatch.setenv("TERMINAL_CWD", str(runtime))

        restore = self._seed_stale_default_env(runtime)
        try:
            observed: dict = {}
            self._install_capturing_agent(monkeypatch, observed)

            wd_job = {
                "id": "seq-wd",
                "name": "seq-wd",
                "workdir": str(workdir),
                "model": "test-model",
                "schedule_display": "manual",
            }
            ok1, *_ = sched.run_job(wd_job)
            assert ok1 is True
            assert observed["base_dir"] == str(workdir.resolve())

            # The shared env is back at the runtime checkout between jobs.
            assert Path(tt._active_environments["default"].cwd).resolve() == runtime.resolve()

            no_wd_job = {
                "id": "seq-nowd",
                "name": "seq-nowd",
                "workdir": None,
                "model": "test-model",
                "schedule_display": "manual",
            }
            ok2, *_ = sched.run_job(no_wd_job)
            assert ok2 is True
            # No leak: the workdir-less job resolves to the runtime checkout, not
            # the previous job's workdir.
            assert observed["base_dir"] == str(runtime.resolve())
            assert Path(observed["live_env_cwd"]).resolve() == runtime.resolve()
        finally:
            restore()

    def test_lazily_created_env_does_not_leak_workdir(self, tmp_path, monkeypatch):
        """Cold-start path: NO "default" env exists when the workdir job starts,
        but a terminal command lazily creates one (seeded from TERMINAL_CWD =
        workdir) during the run. run_job must neutralise that env on cleanup so
        it does not leak the workdir into a later workdir-less job.
        """
        import sys
        from pathlib import Path

        import cron.scheduler as sched
        import tools.file_tools as ft
        import tools.terminal_tool as tt

        workdir = tmp_path / "worktree"
        workdir.mkdir()
        runtime = tmp_path / "runtime"
        runtime.mkdir()

        monkeypatch.setenv("HERMES_MODEL", "test-model")
        # TERMINAL_CWD starts at the runtime checkout (a workdir-less default).
        monkeypatch.setenv("TERMINAL_CWD", str(runtime))

        class _FakeEnv:
            def __init__(self, cwd):
                self.cwd = cwd

        # Start with a genuinely empty "default" env slot (snapshot + restore).
        _sentinel = object()
        prior_env = tt._active_environments.get("default", _sentinel)
        prior_override = tt._task_env_overrides.get("default", _sentinel)
        prior_fileops = ft._file_ops_cache.get("default", _sentinel)
        prior_last_known = ft._last_known_cwd.get("default", _sentinel)
        tt._active_environments.pop("default", None)
        tt._task_env_overrides.pop("default", None)
        ft._file_ops_cache.pop("default", None)
        ft._last_known_cwd.pop("default", None)
        try:
            observed: dict = {}
            TestRunJobTerminalCwd._install_stubs(monkeypatch, observed)

            class LazyEnvAgent:
                def __init__(self, **kwargs):
                    pass

                def run_conversation(self, *_a, **_kw):
                    # Mimic the first terminal command of the job creating the
                    # shared env seeded from TERMINAL_CWD (= the workdir).
                    tt._active_environments["default"] = _FakeEnv(
                        os.environ.get("TERMINAL_CWD")
                    )
                    return {"final_response": "done", "messages": []}

                def get_activity_summary(self):
                    return {"seconds_since_activity": 0.0}

            fake_mod = type(sys)("run_agent")
            fake_mod.AIAgent = LazyEnvAgent
            monkeypatch.setitem(sys.modules, "run_agent", fake_mod)

            wd_job = {
                "id": "lazy-wd",
                "name": "lazy-wd",
                "workdir": str(workdir),
                "model": "test-model",
                "schedule_display": "manual",
            }
            ok, *_ = sched.run_job(wd_job)
            assert ok is True

            # The env was created mid-run with cwd = workdir; cleanup must have
            # cleared that cwd so it no longer shadows a later job's resolution.
            lazy = tt._active_environments.get("default")
            assert lazy is not None
            assert lazy.cwd is None, f"leaked workdir cwd: {lazy.cwd!r}"
            # Concretely: a relative write now resolves to the runtime checkout
            # (TERMINAL_CWD restored), NOT the previous job's workdir.
            assert ft._resolve_base_dir("default") == Path(str(runtime)).resolve()
        finally:
            for store, key, val in (
                (tt._active_environments, "default", prior_env),
                (tt._task_env_overrides, "default", prior_override),
                (ft._file_ops_cache, "default", prior_fileops),
                (ft._last_known_cwd, "default", prior_last_known),
            ):
                if val is _sentinel:
                    store.pop(key, None)
                else:
                    store[key] = val
