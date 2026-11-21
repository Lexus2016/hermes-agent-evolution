"""Tests for cron job re-pin workflow and auto-pinning (#3095)."""

from unittest.mock import MagicMock, patch

import cron.jobs as cron_jobs
from hermes_cli.cron import _JOB_ARG_FIELDS, cron_command


def _create_test_job(tmp_path, **kwargs):
    job = {
        "id": "job-1",
        "name": "test job",
        "prompt": "do something",
        "schedule": {"kind": "interval", "minutes": 10, "display": "every 10m"},
        "enabled": True,
        "state": "scheduled",
        "deliver": "local",
        "provider": None,
        "model": None,
        "provider_snapshot": "anthropic",
        "model_snapshot": "claude-3-5-sonnet",
        "last_status": "drift_skip",
        "last_error": "[drift_skip] provider drifted",
        "drift_alerted": True,
    }
    job.update(kwargs)
    with cron_jobs.use_cron_store(tmp_path):
        cron_jobs.save_jobs([job])
    return job


class TestCronRepin:
    def test_repin_updates_snapshot_and_clears_drift_flags(self, tmp_path):
        _create_test_job(tmp_path)

        with cron_jobs.use_cron_store(tmp_path),              patch("cron.jobs._compute_provider_model_snapshots", return_value=("openrouter", "deepseek/deepseek-chat")):
            updated = cron_jobs.repin_jobs(job_ids=["job-1"])
            assert len(updated) == 1
            job = cron_jobs.get_job("job-1")
            assert job["provider_snapshot"] == "openrouter"
            assert job["model_snapshot"] == "deepseek/deepseek-chat"
            assert job.get("drift_alerted") is None
            assert job.get("last_status") is None

    def test_repin_pin_explicitly(self, tmp_path):
        _create_test_job(tmp_path)

        with cron_jobs.use_cron_store(tmp_path),              patch("cron.jobs._compute_provider_model_snapshots", return_value=("openrouter", "deepseek/deepseek-chat")):
            updated = cron_jobs.repin_jobs(job_ids=["job-1"], pin_explicitly=True)
            assert len(updated) == 1
            job = cron_jobs.get_job("job-1")
            assert job["provider"] == "openrouter"
            assert job["model"] == "deepseek/deepseek-chat"
            assert job["provider_snapshot"] is None
            assert job["model_snapshot"] is None
            assert job.get("drift_alerted") is None

    def test_create_job_pin_inference(self, tmp_path):
        with cron_jobs.use_cron_store(tmp_path),              patch("cron.jobs._compute_provider_model_snapshots", return_value=("nous", "hermes-3-llama-3.1-405b")):
            job = cron_jobs.create_job(
                prompt="hello",
                schedule="every 1h",
                pin_inference=True,
            )
            assert job["provider"] == "nous"
            assert job["model"] == "hermes-3-llama-3.1-405b"
            assert job["provider_snapshot"] is None
            assert job["model_snapshot"] is None

    def test_repin_failsafe_when_resolution_fails(self, tmp_path):
        _create_test_job(tmp_path)

        with cron_jobs.use_cron_store(tmp_path),              patch("cron.jobs._compute_provider_model_snapshots", return_value=(None, None)):
            updated = cron_jobs.repin_jobs(job_ids=["job-1"])
            assert len(updated) == 0
            job = cron_jobs.get_job("job-1")
            # Snapshots and flags must remain intact to prevent silent unspendable state
            assert job["provider_snapshot"] == "anthropic"
            assert job["model_snapshot"] == "claude-3-5-sonnet"
            assert job.get("drift_alerted") is True

    def test_repin_specific_job_with_drifted_only_ignores_healthy(self, tmp_path):
        _create_test_job(
            tmp_path,
            provider_snapshot="openrouter",
            model_snapshot="deepseek/deepseek-chat",
            last_status="success",
            last_error=None,
            drift_alerted=False,
        )

        with cron_jobs.use_cron_store(tmp_path),              patch("cron.jobs._compute_provider_model_snapshots", return_value=("openrouter", "deepseek/deepseek-chat")):
            updated = cron_jobs.repin_jobs(job_ids=["job-1"], drifted_only=True)
            assert len(updated) == 0

    def test_repin_cli_command(self, tmp_path, capsys):
        _create_test_job(tmp_path)
        args = MagicMock()
        args.cron_command = "repin"
        args.job_id = "job-1"
        args.all = False
        args.drifted = False
        args.pin = False

        with cron_jobs.use_cron_store(tmp_path),              patch("cron.jobs._compute_provider_model_snapshots", return_value=("openrouter", "deepseek/deepseek-chat")):
            code = cron_command(args)
            assert code == 0
            out = capsys.readouterr().out
            assert "Re-pinned baseline for 1 job" in out
            assert "job-1" in out

    def test_repin_cli_rejects_both_job_id_and_all(self, tmp_path, capsys):
        _create_test_job(tmp_path)
        args = MagicMock()
        args.cron_command = "repin"
        args.job_id = "job-1"
        args.all = True
        args.drifted = False
        args.pin = False

        with cron_jobs.use_cron_store(tmp_path):
            code = cron_command(args)
            assert code == 1
            out = capsys.readouterr().out
            assert "Cannot specify both a job ID and --all" in out

    def test_cli_create_with_pin(self, tmp_path, capsys):
        args = MagicMock()
        args.cron_command = "create"
        args.schedule = "every 2h"
        args.prompt = "check server"
        args.name = "server check"
        args.deliver = "local"
        args.repeat = None
        args.skill = None
        args.skills = None
        args.script = None
        args.workdir = None
        args.model = None
        args.model_provider = None
        args.no_agent = False
        args.monitor_script = None
        args.monitor_url = None
        args.continuity = None
        # The CLI's pin flag is ``--pin`` with dest ``pinned``: it locks the main
        # agent model onto the job (the fork's ``pin_inference`` spelling is gone).
        args.pinned = True
        args.paused = False
        args.paused_reason = None
        # Every field _job_api_kwargs forwards must be a real value: a MagicMock
        # auto-attribute would be rejected by create_job's validators (timezone,
        # catch_up, reasoning_effort) before anything is created.
        for _api_key, _attr in _JOB_ARG_FIELDS:
            if isinstance(getattr(args, _attr), MagicMock):
                setattr(args, _attr, None)

        # Only the pin resolution is faked: the real snapshot helper is what
        # must leave a pinned axis snapshot-less.
        with cron_jobs.use_cron_store(tmp_path),              patch("cron.jobs._main_model_pin", return_value=("nous", "hermes-3-llama-3.1-405b")):
            code = cron_command(args)
            assert code == 0
            jobs = cron_jobs.load_jobs()
            assert len(jobs) == 1
            assert jobs[0]["provider"] == "nous"
            assert jobs[0]["model"] == "hermes-3-llama-3.1-405b"
            assert jobs[0]["provider_snapshot"] is None

    def test_resnap_cli_adopts_current_baseline(self, tmp_path, capsys):
        """`hermes cron resnap <id>` refreshes snapshots without pinning (#44585)."""
        _create_test_job(tmp_path)
        args = MagicMock()
        args.cron_command = "resnap"
        args.job_id = "job-1"
        args.all = False

        with cron_jobs.use_cron_store(tmp_path),              patch("cron.jobs._compute_provider_model_snapshots", return_value=("nous", "hermes-4")):
            code = cron_command(args)
            assert code == 0
            job = cron_jobs.get_job("job-1")
        assert "Re-snapped 1 job" in capsys.readouterr().out
        assert job["provider_snapshot"] == "nous" and job["model_snapshot"] == "hermes-4"
        assert job["provider"] is None and job["model"] is None, "resnap must never pin"

    def test_repin_drifted_alone_targets_every_drifted_job(self, tmp_path, capsys):
        """`repin --drifted` with no job ID sweeps the drifted set, not an error."""
        drifted_job = {
            "id": "drifted-one",
            "name": "drifted",
            "prompt": "p",
            "schedule": {"kind": "interval", "minutes": 10, "display": "every 10m"},
            "enabled": True,
            "state": "scheduled",
            "deliver": "local",
            "provider": None,
            "model": None,
            "provider_snapshot": "anthropic",
            "model_snapshot": "claude-3-5-sonnet",
            "last_status": "drift_skip",
            "last_error": "[drift_skip] provider drifted",
            "drift_alerted": True,
        }
        healthy_job = {
            **drifted_job,
            "id": "healthy-one",
            "name": "healthy",
            # Already on the current baseline the patched resolver reports.
            "provider_snapshot": "nous",
            "model_snapshot": "hermes-4",
            "last_status": "success",
            "last_error": None,
            "drift_alerted": False,
        }
        args = MagicMock()
        args.cron_command = "repin"
        args.job_id = None
        args.all = False
        args.drifted = True
        args.pin = False

        with cron_jobs.use_cron_store(tmp_path):
            cron_jobs.save_jobs([drifted_job, healthy_job])
            with patch("cron.jobs._compute_provider_model_snapshots",
                       return_value=("nous", "hermes-4")):
                code = cron_command(args)
            healthy = cron_jobs.get_job("healthy-one")
        assert code == 0
        out = capsys.readouterr().out
        assert "Re-pinned baseline for 1 job" in out
        assert "drifted-one" in out and "healthy-one" not in out
        assert healthy["provider_snapshot"] == "nous", "healthy job untouched"
