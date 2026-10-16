"""Regression for #3474: real persisted premises fence deferred side effects."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone, UTC
from pathlib import Path

import pytest

from cron import jobs, scheduler
from cron.decision_ledger import check
from hermes_constants import set_hermes_home_override, reset_hermes_home_override
from scripts.evolution_funnel import compute_funnel


@contextmanager
def profile(home):
    home.mkdir(exist_ok=True)
    token = set_hermes_home_override(home)
    try:
        with jobs.use_cron_store(home):
            yield
    finally:
        reset_hermes_home_override(token)


@pytest.mark.platforms("linux")
def test_changed_evidence_blocks_scripts_and_stays_reopened_across_profiles(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "a"))
    a, b = tmp_path / "a", tmp_path / "b"
    with profile(a):
        evidence = a / "premise.txt"
        evidence.write_text("ready")
        (a / "scripts").mkdir()
        marker = a / "ran"
        (a / "scripts" / "task.py").write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
        job = jobs.create_job("Act on ready", "every 1h", script="task.py", no_agent=True,
                              decision={"watch_files": [str(evidence)]})
        assert check(jobs.get_job(job["id"])["decision_ledger"]) is None
        assert scheduler.run_job(jobs.get_job(job["id"]))[0]
        assert marker.exists()
        marker.unlink()
        evidence.write_text("cancelled")
        # Run the real scheduler, before any script/monitor/agent preparation.
        ok, _, _, error = scheduler.run_job(jobs.get_job(job["id"]))
        assert not ok and "Decision reopened" in error
        assert not marker.exists()
        jobs.mark_job_run(job["id"], False, error)
        stored = jobs.get_job(job["id"])
        assert stored["state"] == "paused" and not stored["enabled"]
        assert stored["repeat"]["completed"] == 0
        assert stored["decision_ledger"]["state"] == "reopened"
        assert compute_funnel(a / "evolution", datetime.now(UTC).date().isoformat())["decisions_reopened"] == 1
        jobs.resume_job(job["id"])
        assert not scheduler.run_job(jobs.get_job(job["id"]))[0]
        assert len(list((a / "evolution" / "decision-reopens").glob("*.json"))) == 1
    with profile(b):
        assert jobs.get_job(job["id"]) is None
        assert not (b / "evolution").exists()
        with pytest.raises(ValueError, match="active profile"):
            jobs.create_job("Act", "every 1h", no_agent=True, script="task.py",
                            decision={"watch_files": [str(evidence)]})
    with profile(a):
        one = jobs.create_job("Act once", (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                              script="task.py", no_agent=True,
                              decision={"watch_files": [str(evidence)]})
        assert jobs.claim_dispatch(one["id"])
        evidence.write_text("changed again")
        result = scheduler.run_job(jobs.get_job(one["id"]))
        assert not result[0]
        jobs.mark_job_run(one["id"], False, result[3])
        assert jobs.get_job(one["id"])["state"] == "paused"
        assert jobs.get_job(one["id"])["repeat"]["completed"] == 0
        # A manual attempt without a dispatch claim must not undo earlier work.
        manual = jobs.create_job("Manual", "every 1h", script="task.py", no_agent=True,
                                 decision={"watch_files": [str(evidence)]})
        jobs.mark_job_run(manual["id"], True)
        evidence.write_text("manual premise changed")
        result = scheduler.run_job(jobs.get_job(manual["id"]))
        jobs.mark_job_run(manual["id"], False, result[3])
        assert jobs.get_job(manual["id"])["repeat"]["completed"] == 1
        # Only an explicit replan refreshes the snapshot; it does not resume.
        replanned = jobs.update_job(job["id"], {"decision": {"watch_files": [str(evidence)]}})
        assert replanned["state"] == "paused"
        assert check(replanned["decision_ledger"]) is None
        assert replanned["decision_ledger"]["id"] != job["decision_ledger"]["id"]


@pytest.mark.platforms("linux")
def test_unavailable_stale_and_escaping_evidence_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    with profile(tmp_path / "profile"):
        root = tmp_path / "profile"
        evidence = root / "premise"
        evidence.write_text("ready")
        job = jobs.create_job("Act", "every 1h", no_agent=True, script="task.py",
                              decision={"watch_files": [str(evidence)], "max_age_seconds": 60})
        ledger = job["decision_ledger"]
        old = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        assert check({**ledger, "created_at": old}) is not None
        assert check({**ledger, "watch_conditions": [{}]}) is not None
        evidence.unlink()
        assert check(ledger) is not None
        outside = tmp_path / "outside"
        outside.write_text("ready")
        evidence.symlink_to(outside)
        assert check(ledger) is not None
        evidence.unlink()
        evidence.write_bytes(b"x" * (1024 * 1024 + 1))
        assert check(ledger) is not None
        with pytest.raises(ValueError):
            jobs.update_job(job["id"], {"decision_ledger": {"state": "active"}})
        # Missing ledger is compatibility, not a claim that evidence was checked.
        legacy = dict(job)
        del legacy["decision_ledger"]
        from cron.decision_ledger import preflight
        assert preflight(legacy) is None


@pytest.mark.platforms("linux")
def test_replan_between_submission_and_owner_blocks_script(tmp_path, monkeypatch, cron_owner):
    """Exercise the real owner execution seam, not only the legacy local runner."""
    with profile(tmp_path / "profile"):
        home = tmp_path / "profile"
        evidence = home / "premise"
        evidence.write_text("ready")
        (home / "scripts").mkdir()
        marker = home / "ran"
        (home / "scripts" / "prepare.py").write_text(
            f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
        job = jobs.create_job("Act", "every 1h", script="prepare.py",
                              decision={"watch_files": [str(evidence)]})
        newer = []

        def replan_then_execute(snapshot, **kwargs):
            evidence.write_text("new plan")
            newer.append(jobs.update_job(job["id"], {
                "decision": {"watch_files": [str(evidence)]}}))
            return cron_owner(snapshot, **kwargs)

        monkeypatch.setattr("cron.scheduler_authority.run_canonical_job", replan_then_execute)
        result = scheduler.run_job(job)
        assert not result[0]
        assert "replanned" in result[3]
        assert not marker.exists()
        current = jobs.get_job(job["id"])
        assert current["decision_ledger"] == newer[0]["decision_ledger"]
        assert current["enabled"]
        assert not (home / "evolution" / "decision-reopens").exists()


def test_readonly_admission_check_never_pauses_or_adopts_replan(tmp_path):
    from cron.decision_ledger import preflight_current

    with profile(tmp_path / "profile"):
        job = jobs.create_job("Act", "every 1h", decision={"max_age_seconds": 60})
        assert preflight_current(job, persist=False) is None
        newer = jobs.update_job(job["id"], {"decision": {}})
        assert "replanned" in preflight_current(job, persist=False)
        assert jobs.get_job(job["id"])["decision_ledger"] == newer["decision_ledger"]
        legacy = dict(newer)
        legacy.pop("decision_ledger")
        assert "replanned" in preflight_current(legacy, persist=False)
        records = jobs.load_jobs()
        records[0].pop("decision_ledger")
        jobs.save_jobs(records)
        assert "replanned" in preflight_current(newer, persist=False)
        assert preflight_current(legacy, persist=False) is None


@pytest.mark.platforms("linux")
def test_changed_evidence_refused_before_canonical_submission(tmp_path, monkeypatch):
    from unittest.mock import Mock

    from cron.decision_ledger import preflight_current

    with profile(tmp_path / "profile"):
        evidence = tmp_path / "profile" / "premise"
        evidence.write_text("ready")
        job = jobs.create_job("Act", "every 1h", decision={"watch_files": [str(evidence)]})
        evidence.write_text("cancelled")
        assert "Decision reopened" in preflight_current(job, persist=False)
        assert jobs.get_job(job["id"])["enabled"]
        submit = Mock(side_effect=AssertionError("must fence before transport"))
        monkeypatch.setattr("cron.scheduler_authority.run_canonical_job", submit)
        assert not scheduler.run_job(job)[0]
        submit.assert_not_called()
        assert not jobs.get_job(job["id"])["enabled"]


def test_readonly_fence_does_not_repair_corrupt_store(tmp_path, monkeypatch):
    from cron.decision_ledger import preflight_current

    with profile(tmp_path / "profile"):
        job = jobs.create_job("Act", "every 1h")
        store = jobs._current_cron_store().jobs_file
        store.write_text('{"jobs": "corrupt"}')
        before = store.read_bytes()
        monkeypatch.setattr(jobs, "load_jobs", lambda: pytest.fail("repairing read used"))
        assert "malformed" in preflight_current(job, persist=False)
        assert store.read_bytes() == before


def test_reopened_decision_records_canonical_completion_once(tmp_path):
    from cron.decision_ledger import preflight

    with profile(tmp_path / "profile"):
        job = jobs.create_job("Act", "every 1h")
        records = jobs.load_jobs()
        records[0]["decision_ledger"]["created_at"] = "invalid"
        jobs.save_jobs(records)
        assert preflight(jobs.get_job(job["id"]))
        assert jobs.mark_job_run(job["id"], False, "refused", execution_id="execution-one")
        first = jobs.get_job(job["id"])
        assert first["canonical_completions"] == ["execution-one"]
        assert jobs.mark_job_run(job["id"], False, "duplicate", execution_id="execution-one")
        assert jobs.get_job(job["id"]) == first
