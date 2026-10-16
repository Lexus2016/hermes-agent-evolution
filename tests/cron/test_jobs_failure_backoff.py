"""Tests for cron/jobs.py — schedule parsing, job CRUD, and due-job detection."""

import threading
import pytest
from datetime import datetime, timedelta, timezone, UTC

from cron.jobs import (
    parse_duration,
    parse_schedule,
    compute_next_run,
    create_job,
    load_jobs,
    save_jobs,
    get_job,
    list_jobs,
    update_job,
    pause_job,
    resume_job,
    trigger_job,
    remove_job,
    mark_job_run,
    advance_next_run,
    claim_dispatch,
    claim_job_for_fire,
    heartbeat_run_claim,
    get_due_jobs,
    save_job_output,
    _hermes_now,
    _advance_schedule_by_steps,
    _failure_backoff_config,
)


# =========================================================================
# parse_duration
# =========================================================================


@pytest.fixture()
def tmp_cron_dir(tmp_path, monkeypatch):
    """Redirect cron storage to a temp directory."""
    monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")
    return tmp_path


class TestFailureBackoff:
    """#70: per-job exponential backoff + circuit breaker for deterministically
    failing jobs. An error byte-identical to the previous run is deterministic
    — the fixed schedule re-running it only burns tokens/disk. Backoff skips
    2**(streak-1)-1 slots (capped); past the threshold the job auto-pauses."""

    def test_single_failure_sets_streak_one_no_backoff(self, tmp_cron_dir):
        job = create_job(prompt="Fail", schedule="every 1h")
        mark_job_run(job["id"], success=False, error="KeyError: 'foo'")
        updated = get_job(job["id"])
        assert updated["failure_streak"] == 1
        assert updated["failure_backoff_skips"] == 0
        assert updated["enabled"] is True
        assert updated["state"] == "scheduled"

    def test_identical_failures_increment_streak_and_backoff(self, tmp_cron_dir):
        job = create_job(prompt="Fail", schedule="every 10m")
        mark_job_run(job["id"], success=False, error="KeyError: 'foo'")
        mark_job_run(job["id"], success=False, error="KeyError: 'foo'")
        updated = get_job(job["id"])
        assert updated["failure_streak"] == 2
        assert updated["failure_backoff_skips"] == 1  # 2**(2-1)-1

    def test_changed_error_resets_streak(self, tmp_cron_dir):
        job = create_job(prompt="Fail", schedule="every 10m")
        mark_job_run(job["id"], success=False, error="KeyError: 'foo'")
        mark_job_run(job["id"], success=False, error="TypeError: bad op")
        updated = get_job(job["id"])
        assert updated["failure_streak"] == 1

    def test_success_resets_streak_and_skips(self, tmp_cron_dir):
        job = create_job(prompt="Fail", schedule="every 10m")
        mark_job_run(job["id"], success=False, error="KeyError: 'foo'")
        mark_job_run(job["id"], success=False, error="KeyError: 'foo'")
        mark_job_run(job["id"], success=True)
        updated = get_job(job["id"])
        assert updated["failure_streak"] == 0
        assert updated.get("failure_backoff_skips") in (None, 0)

    def test_auto_pause_after_threshold_identical_failures(self, tmp_cron_dir):
        job = create_job(prompt="Fail", schedule="every 1h")
        for _ in range(10):
            mark_job_run(job["id"], success=False, error="KeyError: 'foo'")
        updated = get_job(job["id"])
        assert updated["failure_streak"] == 10
        assert updated["enabled"] is False
        assert updated["state"] == "paused"
        assert updated["next_run_at"] is None
        assert "auto-paused" in (updated.get("paused_reason") or "")

    def test_resume_clears_streak(self, tmp_cron_dir):
        job = create_job(prompt="Fail", schedule="every 1h")
        for _ in range(10):
            mark_job_run(job["id"], success=False, error="KeyError: 'foo'")
        resume_job(job["id"])
        updated = get_job(job["id"])
        assert updated["enabled"] is True
        assert updated["failure_streak"] == 0

    def test_advance_schedule_by_steps_interval(self):
        base = datetime(2026, 8, 18, 12, 0, tzinfo=UTC).isoformat()
        advanced = _advance_schedule_by_steps(
            {"kind": "interval", "minutes": 10}, base, 3
        )
        dt = datetime.fromisoformat(advanced)
        assert dt == datetime(2026, 8, 18, 12, 30, tzinfo=UTC)

    def test_advance_schedule_by_steps_zero_is_identity(self):
        base = "2026-08-18T12:00:00+00:00"
        assert (
            _advance_schedule_by_steps({"kind": "interval", "minutes": 10}, base, 0)
            == base
        )

    def test_failure_backoff_config_returns_dict_with_defaults(self, tmp_cron_dir):
        # Even with no explicit tuning, the config section is a dict and the
        # call sites fall back to their built-in defaults.
        cfg = _failure_backoff_config()
        assert isinstance(cfg, dict)
        assert cfg.get("failure_backoff_max_skips", 16) == 16

    def test_non_int_max_skips_config_falls_back_to_default(
        self, tmp_cron_dir, monkeypatch
    ):
        """A non-int ``cron.failure_backoff_max_skips`` must not crash the
        failure path — the built-in default (16) applies instead."""
        monkeypatch.setattr(
            "cron.jobs._failure_backoff_config",
            lambda: {
                "failure_backoff_max_skips": "not-an-int",
                "failure_auto_pause_threshold": 10,
            },
        )
        job = create_job(prompt="Fail", schedule="every 10m")
        for _ in range(5):
            mark_job_run(job["id"], success=False, error="KeyError: 'foo'")
        updated = get_job(job["id"])
        assert updated["failure_streak"] == 5
        # 2**(5-1)-1 = 15, capped at the default 16 — no crash, no cap blowout.
        assert updated["failure_backoff_skips"] == 15

    def test_non_int_pause_threshold_config_falls_back_to_default(
        self, tmp_cron_dir, monkeypatch
    ):
        """A non-int ``cron.failure_auto_pause_threshold`` must not crash the
        failure path — auto-pause still trips at the built-in default (10)."""
        monkeypatch.setattr(
            "cron.jobs._failure_backoff_config",
            lambda: {
                "failure_backoff_max_skips": 16,
                "failure_auto_pause_threshold": "oops",
            },
        )
        job = create_job(prompt="Fail", schedule="every 1h")
        for _ in range(10):
            mark_job_run(job["id"], success=False, error="KeyError: 'foo'")
        updated = get_job(job["id"])
        assert updated["enabled"] is False
        assert updated["state"] == "paused"
        assert "auto-paused" in (updated.get("paused_reason") or "")

    def test_interruption_marked_failure_not_counted_in_streak(self, tmp_cron_dir):
        """A synthetic interruption mark (scheduler restart) must not advance
        the failure streak — restarts are not evidence of a deterministic
        job failure."""
        job = create_job(prompt="Fail", schedule="every 10m")
        mark_job_run(job["id"], success=False, error="KeyError: 'foo'")
        mark_job_run(job["id"], success=False, error="KeyError: 'foo'")
        assert get_job(job["id"])["failure_streak"] == 2
        mark_job_run(
            job["id"],
            success=False,
            error="Interrupted by shutdown before terminal completion.",
            status="interrupted",
        )
        updated = get_job(job["id"])
        assert updated["failure_streak"] == 2, (
            "interruption mark must not increment the streak"
        )
        assert updated["enabled"] is True
        # A subsequent REAL identical failure resumes normal accounting. The
        # synthetic mark replaced last_error, so the byte-identity comparison
        # restarts the streak at 1 — conservative, and it can never
        # false-positive auto-pause after a restart.
        mark_job_run(job["id"], success=False, error="KeyError: 'foo'")
        assert get_job(job["id"])["failure_streak"] == 1

    def test_interruption_mark_cannot_trip_auto_pause(self, tmp_cron_dir):
        """Scheduler-restart marks must not false-positive auto-pause: a job
        one failure below the threshold stays live through a restart mark."""
        job = create_job(prompt="Fail", schedule="every 1h")
        for _ in range(9):
            mark_job_run(job["id"], success=False, error="KeyError: 'foo'")
        assert get_job(job["id"])["failure_streak"] == 9
        assert get_job(job["id"])["enabled"] is True
        mark_job_run(
            job["id"],
            success=False,
            error="Interrupted by shutdown before terminal completion.",
            status="interrupted",
        )
        updated = get_job(job["id"])
        assert updated["failure_streak"] == 9
        assert updated["enabled"] is True
        assert updated["state"] == "scheduled"

    def test_trigger_job_clears_streak(self, tmp_cron_dir):
        """`cronjob run` / `hermes cron run` (trigger_job) is a re-enable:
        it must clear the streak so one immediate failure doesn't re-trip."""
        job = create_job(prompt="Fail", schedule="every 1h")
        for _ in range(10):
            mark_job_run(job["id"], success=False, error="KeyError: 'foo'")
        assert get_job(job["id"])["enabled"] is False
        trigger_job(job["id"])
        updated = get_job(job["id"])
        assert updated["enabled"] is True
        assert updated["failure_streak"] == 0
        assert updated.get("failure_backoff_skips") in (None, 0)

    def test_forced_fire_claim_clears_streak(self, tmp_cron_dir):
        """A forced fire on a paused job (claim_job_for_fire(force=True) —
        trigger-now) re-enables it and must clear the streak."""
        job = create_job(prompt="Fail", schedule="every 1h")
        for _ in range(10):
            mark_job_run(job["id"], success=False, error="KeyError: 'foo'")
        assert get_job(job["id"])["enabled"] is False
        claimed = claim_job_for_fire(job["id"], force=True, return_job=True)
        assert isinstance(claimed, dict)
        assert claimed["enabled"] is True
        assert claimed["failure_streak"] == 0
        assert claimed.get("failure_backoff_skips") in (None, 0)

    def test_update_job_re_enable_clears_streak(self, tmp_cron_dir):
        """An update that transitions the job back into an enabled, scheduled
        state is a re-enable and must clear the streak."""
        job = create_job(prompt="Fail", schedule="every 1h")
        for _ in range(10):
            mark_job_run(job["id"], success=False, error="KeyError: 'foo'")
        assert get_job(job["id"])["enabled"] is False
        updated = update_job(job["id"], {"enabled": True, "state": "scheduled"})
        assert updated is not None
        assert updated["enabled"] is True
        assert updated["failure_streak"] == 0
        assert updated.get("failure_backoff_skips") in (None, 0)

    def test_plain_update_keeps_streak(self, tmp_cron_dir):
        """An update that does NOT re-enable (already-live job, unrelated
        field) must leave the failure streak intact."""
        job = create_job(prompt="Fail", schedule="every 10m")
        mark_job_run(job["id"], success=False, error="KeyError: 'foo'")
        mark_job_run(job["id"], success=False, error="KeyError: 'foo'")
        updated = update_job(job["id"], {"prompt": "Fail harder"})
        assert updated is not None
        assert updated["failure_streak"] == 2
