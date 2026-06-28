"""Tests for cron failure logging / per-job failure records / digest (issue #433).

These tests exercise the focused first slice added to cron/scheduler.py and
cron/jobs.py:

* ``save_job_failure`` / ``list_job_failures`` / ``get_latest_failure`` persistence
* ``run_one_job`` writes a failure record on agent/script failure
* ``run_one_job`` writes a success marker on recovery
* ``build_cron_failure_digest`` respects the ``cron.failure_digest`` config key
* failure records include last-N output and traceback
"""

import contextlib
import json
import logging
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import cron.jobs as jobs
import cron.scheduler as scheduler
from cron.scheduler import build_cron_failure_digest


@pytest.fixture(autouse=True)
def _patch_hermes_home(tmp_path, monkeypatch):
    """Redirect HERMES_HOME and scheduler's internal override to a temp dir."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    scheduler._hermes_home = tmp_path
    jobs.HERMES_DIR = tmp_path
    jobs.CRON_DIR = tmp_path / "cron"
    jobs.OUTPUT_DIR = jobs.CRON_DIR / "output"
    jobs.FAILURE_DIR = jobs.CRON_DIR / "failures"
    jobs.JOBS_FILE = jobs.CRON_DIR / "jobs.json"
    jobs.TICKER_HEARTBEAT_FILE = jobs.CRON_DIR / "ticker_heartbeat"
    jobs.TICKER_SUCCESS_FILE = jobs.CRON_DIR / "ticker_last_success"
    jobs.ensure_dirs()


def _write_jobs(jobs_list):
    """Persist a raw jobs list directly to the temp jobs.json."""
    jobs.CRON_DIR.mkdir(parents=True, exist_ok=True)
    jobs.JOBS_FILE.write_text(
        json.dumps({"jobs": jobs_list, "updated_at": jobs._hermes_now().isoformat()}),
        encoding="utf-8",
    )


def test_save_job_failure_writes_record(tmp_path):
    job = {"id": "j1", "name": "test job"}
    record_path = jobs.save_job_failure(
        job,
        success=False,
        error="boom",
        output="x" * 5000 + "\nLAST LINE",
        traceback_text="Traceback (most recent call last):\n  ...",
    )

    assert record_path.exists()
    assert jobs.FAILURE_DIR in record_path.parents
    data = json.loads(record_path.read_text(encoding="utf-8"))
    assert data["job_id"] == "j1"
    assert data["job_name"] == "test job"
    assert data["success"] is False
    assert data["error"] == "boom"
    assert "Traceback" in data["traceback"]
    # last-N output trimming
    assert data["last_output"].startswith("...")
    assert "LAST LINE" in data["last_output"]


def test_save_job_failure_success_marker_overwrites_digest_state(tmp_path):
    job = {"id": "j2", "name": "good job"}
    jobs.save_job_failure(job, success=False, error="old")
    path = jobs.save_job_failure(job, success=True, output="ok")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["success"] is True
    assert data["error"] is None


def test_list_and_get_latest_failure(tmp_path):
    job = {"id": "j3", "name": "multi"}
    p1 = jobs.save_job_failure(job, success=False, error="first")
    time.sleep(0.05)
    p2 = jobs.save_job_failure(job, success=False, error="second")

    latest = jobs.get_latest_failure("j3")
    assert latest["error"] == "second"

    all_records = jobs.list_job_failures("j3")
    assert len(all_records) == 2
    assert all_records[0]["error"] == "second"
    assert all_records[1]["error"] == "first"


def test_run_one_job_writes_failure_record_on_agent_failure(monkeypatch):
    def fake_run_job(job):
        return False, "agent output", "", "provider 429 rate limit"

    monkeypatch.setattr(scheduler, "run_job", fake_run_job)
    monkeypatch.setattr(
        scheduler, "save_job_output", lambda jid, out: Path("/tmp/out.md")
    )
    monkeypatch.setattr(scheduler, "_deliver_result", lambda *a, **kw: None)
    monkeypatch.setattr(scheduler, "mark_job_run", lambda *a, **kw: None)

    scheduler.run_one_job({"id": "j4", "name": "fail job"})

    latest = jobs.get_latest_failure("j4")
    assert latest is not None
    assert latest["success"] is False
    assert latest["error"]
    assert "429" in latest["error"]
    assert latest["last_output"] == "agent output"


def test_run_one_job_writes_success_marker(monkeypatch):
    def fake_run_job(job):
        return True, "all good", "final response", None

    monkeypatch.setattr(scheduler, "run_job", fake_run_job)
    monkeypatch.setattr(
        scheduler, "save_job_output", lambda jid, out: Path("/tmp/out.md")
    )
    monkeypatch.setattr(scheduler, "_deliver_result", lambda *a, **kw: None)
    monkeypatch.setattr(scheduler, "mark_job_run", lambda *a, **kw: None)

    scheduler.run_one_job({"id": "j5", "name": "ok job"})

    latest = jobs.get_latest_failure("j5")
    assert latest is not None
    assert latest["success"] is True


def test_failure_digest_disabled_by_default(monkeypatch):
    assert scheduler._failure_digest_enabled({}) is False
    assert (
        scheduler._failure_digest_enabled({"cron": {"failure_digest": False}}) is False
    )
    assert (
        scheduler._failure_digest_enabled({"cron": {"failure_digest": "true"}}) is True
    )


def test_build_digest_respects_failure_digest_config(monkeypatch):
    _write_jobs([{"id": "j6", "name": "digested", "enabled": True}])
    jobs.save_job_failure({"id": "j6", "name": "digested"}, success=False, error="boom")

    # Disabled → no digest
    assert build_cron_failure_digest() is None

    # Enabled → digest emitted and ack timestamp updated
    monkeypatch.setattr(
        scheduler, "_load_cron_config", lambda: {"cron": {"failure_digest": True}}
    )
    digest = build_cron_failure_digest()
    assert digest is not None
    assert "j6" in digest or "digested" in digest
    assert "boom" in digest

    saved = json.loads(jobs.JOBS_FILE.read_text(encoding="utf-8"))
    assert saved["jobs"][0].get("failure_digest_last_at")

    # Same failure is now acked → no second digest
    assert build_cron_failure_digest() is None


def test_build_digest_ignores_success_records_and_old_failures(monkeypatch, tmp_path):
    _write_jobs([{"id": "j7", "name": "mixed", "enabled": True}])
    monkeypatch.setattr(
        scheduler, "_load_cron_config", lambda: {"cron": {"failure_digest": True}}
    )

    jobs.save_job_failure({"id": "j7", "name": "mixed"}, success=True)
    assert build_cron_failure_digest() is None

    # Old failure (timestamp in 2020) should not surface
    old_path = jobs.save_job_failure(
        {"id": "j7", "name": "mixed"}, success=False, error="old"
    )
    data = json.loads(old_path.read_text(encoding="utf-8"))
    data["timestamp"] = "2020-01-01T00:00:00+00:00"
    old_path.write_text(json.dumps(data), encoding="utf-8")
    assert build_cron_failure_digest() is None


def test_run_one_job_failure_record_logs_warning(caplog, monkeypatch):
    def fake_run_job(job):
        return False, "out", "", "bang"

    monkeypatch.setattr(scheduler, "run_job", fake_run_job)
    monkeypatch.setattr(
        scheduler, "save_job_output", lambda jid, out: Path("/tmp/out.md")
    )
    monkeypatch.setattr(scheduler, "_deliver_result", lambda *a, **kw: None)
    monkeypatch.setattr(scheduler, "mark_job_run", lambda *a, **kw: None)

    with caplog.at_level(logging.WARNING, logger="cron.scheduler"):
        scheduler.run_one_job({"id": "j8", "name": "warn job"})

    assert any("failure record saved" in rec.message for rec in caplog.records)
