"""Tests for the interrupted-job marker and startup recovery (issue 105).

A gateway restart used to kill an in-flight cron job with no record at all:
no last_run, no error, no retry — the daily slot silently vanished (the
2026-06-10 incident). mark_job_started() leaves a durable 'running' marker;
recover_interrupted_jobs() — called once at gateway startup — turns stale
markers into a visible 'interrupted' record and re-fires the job once when
the death is recent.
"""

from datetime import timedelta

import pytest

from cron.jobs import (
    _hermes_now,
    create_job,
    load_jobs,
    mark_job_run,
    mark_job_started,
    recover_interrupted_jobs,
)


@pytest.fixture
def tmp_cron_dir(tmp_path, monkeypatch):
    """Redirect cron storage to a temp directory."""
    monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")
    return tmp_path


def _make_job(**over):
    job = create_job(prompt="do things", schedule="0 22 * * *", name="evolution-implementation")
    return job


class TestMarkJobStarted:
    def test_sets_running_state_and_timestamp(self, tmp_cron_dir):
        job = _make_job()
        mark_job_started(job["id"])
        stored = load_jobs()[0]
        assert stored["state"] == "running"
        assert stored["run_started_at"]

    def test_mark_job_run_clears_running_state(self, tmp_cron_dir):
        job = _make_job()
        mark_job_started(job["id"])
        mark_job_run(job["id"], success=True)
        stored = load_jobs()[0]
        assert stored["state"] == "scheduled"
        assert stored["last_status"] == "ok"

    def test_unknown_job_id_is_noop(self, tmp_cron_dir):
        _make_job()
        mark_job_started("does-not-exist")  # must not raise
        assert load_jobs()[0]["state"] == "scheduled"


class TestRecoverInterruptedJobs:
    def test_recent_interruption_refires_once(self, tmp_cron_dir):
        job = _make_job()
        mark_job_started(job["id"])
        # Simulate "gateway restarted shortly after the job began".
        recovered = recover_interrupted_jobs(max_refire_age_hours=6.0)

        assert len(recovered) == 1
        stored = load_jobs()[0]
        assert stored["state"] == "scheduled"
        assert stored["last_status"] == "interrupted"
        assert "restart" in (stored["last_error"] or "")
        # Re-fired: next_run_at pulled to ~now (well before the 22:00 slot).
        now = _hermes_now()
        from datetime import datetime

        next_run = datetime.fromisoformat(stored["next_run_at"])
        assert abs((next_run - now).total_seconds()) < 120

    def test_old_interruption_records_but_does_not_refire(self, tmp_cron_dir):
        job = _make_job()
        mark_job_started(job["id"])
        # Backdate the marker beyond the refire window.
        jobs = load_jobs()
        old = (_hermes_now() - timedelta(hours=20)).isoformat()
        jobs[0]["run_started_at"] = old
        from cron.jobs import save_jobs

        save_jobs(jobs)
        next_before = load_jobs()[0]["next_run_at"]

        recovered = recover_interrupted_jobs(max_refire_age_hours=6.0)

        assert len(recovered) == 1
        stored = load_jobs()[0]
        assert stored["last_status"] == "interrupted"
        assert stored["state"] == "scheduled"
        assert stored["next_run_at"] == next_before  # untouched — waits for its slot

    def test_healthy_jobs_untouched(self, tmp_cron_dir):
        job = _make_job()
        before = load_jobs()[0]
        recovered = recover_interrupted_jobs()
        assert recovered == []
        assert load_jobs()[0] == before

    def test_completed_run_not_treated_as_interrupted(self, tmp_cron_dir):
        job = _make_job()
        mark_job_started(job["id"])
        mark_job_run(job["id"], success=True)
        recovered = recover_interrupted_jobs()
        assert recovered == []
        assert load_jobs()[0]["last_status"] == "ok"
